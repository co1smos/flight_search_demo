const DEFAULTS = Object.freeze({
  baseSha: "HEAD",
  focusedTest: "uv run --with pytest pytest -q",
  finalTest: "uv run --with pytest pytest -q",
  timeoutSeconds: 3600,
});

const SESSION_ID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
const SHA_RE = /^[0-9a-f]{40}$/;
const BRANCH_RE = /^(?!.*\.\.)(?!.*\/\/)(?!.*[.~^:?*\[\\])[^\s/][^\s]*[^\s/.]$/;
const EFFORTS = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);

export function parseCliOptions(argv, env = process.env) {
  const values = {
    issueOverride: optionalPositiveInteger(env.SANDCASTLE_ISSUE, "issue"),
    baseSha: env.SANDCASTLE_BASE_SHA || DEFAULTS.baseSha,
    branch: env.SANDCASTLE_BRANCH || undefined,
    model: env.SANDCASTLE_MODEL || undefined,
    effort: env.SANDCASTLE_EFFORT || undefined,
    focusedTest: env.SANDCASTLE_FOCUSED_TEST || DEFAULTS.focusedTest,
    finalTest: env.SANDCASTLE_FINAL_TEST || DEFAULTS.finalTest,
    timeoutMs: parseTimeout(env.SANDCASTLE_TIMEOUT_SECONDS || DEFAULTS.timeoutSeconds),
    maxModelCalls: positiveInteger(env.SANDCASTLE_MAX_MODEL_CALLS || "2", "max model calls"),
    dryRun: parseBoolean(env.SANDCASTLE_DRY_RUN || env.SANDCASTLE_PREFLIGHT || "false"),
  };

  const optionsWithValues = new Map([
    ["--issue", (value) => { values.issueOverride = positiveInteger(value, "issue"); }],
    ["--base-sha", (value) => { values.baseSha = required(value, "base SHA"); }],
    ["--branch", (value) => { values.branch = required(value, "branch"); }],
    ["--model", (value) => { values.model = required(value, "model"); }],
    ["--effort", (value) => { values.effort = required(value, "effort"); }],
    ["--focused-test", (value) => { values.focusedTest = required(value, "focused test"); }],
    ["--final-test", (value) => { values.finalTest = required(value, "final test"); }],
    ["--timeout", (value) => { values.timeoutMs = parseTimeout(value); }],
  ]);

  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (option === "--dry-run" || option === "--preflight") {
      values.dryRun = true;
      continue;
    }
    const assign = optionsWithValues.get(option);
    if (!assign) {
      throw new Error(`unknown option: ${option}`);
    }
    index += 1;
    if (index >= argv.length) {
      throw new Error(`missing value for ${option}`);
    }
    assign(argv[index]);
  }

  if (!values.model || !values.effort) {
    throw new Error("model must be explicitly routed before preflight (set SANDCASTLE_MODEL and SANDCASTLE_EFFORT)");
  }
  if (!EFFORTS.has(values.effort)) {
    throw new Error(`unsupported effort: ${values.effort}`);
  }
  if (values.branch && !BRANCH_RE.test(values.branch)) {
    throw new Error(`invalid branch: ${values.branch}`);
  }
  required(values.focusedTest, "focused test");
  required(values.finalTest, "final test");
  if (values.maxModelCalls !== 2) {
    throw new Error("initial workflow requires exactly 2 model calls: implementer and reviewer");
  }
  return values;
}

export function parseProviderEnvName(configText) {
  const providerMatch = configText.match(/^model_provider\s*=\s*["']([^"']+)["']/m);
  if (!providerMatch) {
    throw new Error("Codex config is missing model_provider");
  }
  const header = `[model_providers.${providerMatch[1]}]`;
  const lines = configText.split(/\r?\n/);
  const start = lines.findIndex((line) => line.trim() === header);
  const section = start < 0
    ? undefined
    : lines.slice(start + 1).findIndex((line) => /^\s*\[/.test(line)) < 0
      ? lines.slice(start + 1).join("\n")
      : lines.slice(start + 1, start + 1 + lines.slice(start + 1).findIndex((line) => /^\s*\[/.test(line))).join("\n");
  const envKey = section?.match(/^env_key\s*=\s*["']([A-Za-z_][A-Za-z0-9_]*)["']/m)?.[1];
  if (!envKey) {
    throw new Error(`Codex provider ${providerMatch[1]} is missing env_key`);
  }
  return envKey;
}

export function declaredBlockerNumbers(body = "") {
  const result = new Set();
  for (const line of body.split(/\r?\n/)) {
    if (!/^\s*(blocked by|depends on)\s*:/i.test(line)) continue;
    for (const match of line.matchAll(/#(\d+)/g)) {
      result.add(Number(match[1]));
    }
  }
  return [...result].sort((left, right) => left - right);
}

export function selectReadyIssue(issues, overrideNumber) {
  if (!Array.isArray(issues)) throw new Error("issue list is invalid");
  const ordered = [...issues].sort((left, right) => left.number - right.number);
  const candidates = overrideNumber === undefined
    ? ordered
    : ordered.filter((issue) => issue.number === overrideNumber);

  if (overrideNumber !== undefined && candidates.length === 0) {
    throw new Error(`override issue #${overrideNumber} was not returned by GitHub`);
  }

  for (const issue of candidates) {
    if (String(issue.state).toUpperCase() !== "OPEN") {
      if (overrideNumber !== undefined) throw new Error(`override issue #${issue.number} is not open`);
      continue;
    }
    if (!issue.labels?.includes("ready-for-agent")) {
      if (overrideNumber !== undefined) {
        throw new Error(`override issue #${issue.number} lacks ready-for-agent`);
      }
      continue;
    }
    const openBlocker = issue.blockers?.find(
      (blocker) => String(blocker.state).toUpperCase() !== "CLOSED",
    );
    if (openBlocker) {
      if (overrideNumber !== undefined) {
        throw new Error(`override issue #${issue.number} has open blocker #${openBlocker.number}`);
      }
      continue;
    }
    return issue;
  }

  throw new Error("no unblocked open ready-for-agent issue is available");
}

export function shellQuote(value) {
  const text = String(value);
  if (/^[A-Za-z0-9_./:=+-]+$/.test(text)) return text;
  return `'${text.replaceAll("'", `'"'"'`)}'`;
}

export function buildCodexPhaseCommand(options) {
  const args = [
    "unsnooze",
    "_run",
    "codex",
    "--ask-for-approval",
    "never",
    "--no-alt-screen",
    "exec",
    "--model",
    options.model,
    "--config",
    `model_reasoning_effort=\"${options.effort}\"`,
    "--sandbox",
    "danger-full-access",
    "--color",
    "never",
    "--cd",
    options.worktreePath,
    "--output-schema",
    options.schemaPath,
    "--output-last-message",
    options.receiptPath,
    "-",
  ];
  return `${args.map(shellQuote).join(" ")} < ${shellQuote(options.promptPath)}`;
}

export function validateImplementerReceipt(receipt, expected) {
  assertRecord(receipt, "implementer receipt");
  expectEqual(receipt.phase, "implementer", "implementer phase");
  expectEqual(receipt.status, "completed", "implementer status");
  expectEqual(receipt.issue_number, expected.issueNumber, "implementer issue number");
  validateSessionId(receipt.session_id);
  validateSha(receipt.head, "implementer head");
  expectEqual(receipt.head, expected.head, "implementer head");
  validateTimestamp(receipt.completed_at, "implementer completed_at");
  return receipt;
}

export function validateReviewerReceipt(receipt, expected) {
  assertRecord(receipt, "reviewer receipt");
  expectEqual(receipt.phase, "reviewer", "reviewer phase");
  expectEqual(receipt.status, "completed", "reviewer status");
  if (!["approved", "changes_requested", "blocked"].includes(receipt.verdict)) {
    throw new Error(`invalid reviewer verdict: ${receipt.verdict}`);
  }
  validateSessionId(receipt.session_id);
  if (receipt.session_id === expected.implementerSessionId) {
    throw new Error("reviewer must use a fresh session");
  }
  validateSha(receipt.reviewed_head, "reviewed head");
  expectEqual(receipt.reviewed_head, expected.reviewedHead, "reviewed head");
  validateTimestamp(receipt.completed_at, "reviewer completed_at");
  if (!Array.isArray(receipt.findings) || receipt.findings.some((finding) => typeof finding !== "string")) {
    throw new Error("reviewer findings must be an array of strings");
  }
  if (receipt.verdict === "approved" && receipt.findings.length > 0) {
    throw new Error("approved reviewer verdict must have no findings");
  }
  return receipt;
}

export function validateSessionEvidence(options) {
  validateSessionId(options.receiptSessionId);
  const paneIds = new Set(options.paneText.match(SESSION_ID_RE) || []);
  if (!paneIds.has(options.receiptSessionId) || paneIds.size !== 1) {
    throw new Error("pane evidence does not identify the exact receipt session");
  }
  const phaseStarted = Date.parse(options.phaseStartedAt);
  if (!Number.isFinite(phaseStarted)) throw new Error("invalid phase start timestamp");
  const matchingRollouts = options.rollouts.filter((rollout) =>
    rollout.sessionId === options.receiptSessionId
      && rollout.cwd === options.worktreePath
      && Date.parse(rollout.startedAt) >= phaseStarted,
  );
  if (matchingRollouts.length !== 1) {
    throw new Error("rollout evidence does not identify exactly one matching session");
  }
  return { rolloutPath: matchingRollouts[0].path };
}

export function hasLingeringUnsnoozeState(statusText, sessionId) {
  return statusText.includes(sessionId) && /\[(RESUMING|STOPPED|HELD)\]/i.test(statusText);
}

function required(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${label} must not be empty`);
  }
  return value;
}

function positiveInteger(value, label) {
  if (!/^\d+$/.test(String(value)) || Number(value) < 1) {
    throw new Error(`${label} must be a positive integer`);
  }
  return Number(value);
}

function optionalPositiveInteger(value, label) {
  return value === undefined || value === "" ? undefined : positiveInteger(value, label);
}

function parseTimeout(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new Error("timeout must be a positive number of seconds");
  }
  return Math.floor(seconds * 1000);
}

function parseBoolean(value) {
  if (["1", "true", "yes"].includes(String(value).toLowerCase())) return true;
  if (["0", "false", "no", ""].includes(String(value).toLowerCase())) return false;
  throw new Error(`invalid boolean: ${value}`);
}

function assertRecord(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
}

function expectEqual(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label} mismatch`);
}

function validateSessionId(value) {
  if (typeof value !== "string" || !new RegExp(`^${SESSION_ID_RE.source}$`, "i").test(value)) {
    throw new Error("invalid Codex session_id");
  }
}

function validateSha(value, label) {
  if (typeof value !== "string" || !SHA_RE.test(value)) throw new Error(`invalid ${label}`);
}

function validateTimestamp(value, label) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) {
    throw new Error(`invalid ${label}`);
  }
}
