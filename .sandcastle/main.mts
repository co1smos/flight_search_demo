import { createSandbox } from "@ai-hero/sandcastle";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import { execFile } from "node:child_process";
import { copyFile, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import {
  buildImplementerRoundContext,
  buildCodexPhaseCommand,
  declaredBlockerNumbers,
  hasLingeringUnsnoozeState,
  parseCliOptions,
  parseProviderEnvName,
  roundArtifactPaths,
  runPhaseWithRetry,
  selectReadyIssue,
  shellQuote,
  validateFreshSessionId,
  validateImplementerReceipt,
  validateReviewerReceipt,
  validateSessionEvidence,
} from "./workflow-core.mjs";

const execFileAsync = promisify(execFile);
const root = process.cwd();
const options = parseCliOptions(process.argv.slice(2));
const runId = `${Date.now()}-${process.pid}`;
const artifactRoot = join(root, ".sandcastle", "runs", runId);

const run = async (file: string, args: string[], cwd = root) => {
  try {
    const result = await execFileAsync(file, args, {
      cwd,
      encoding: "utf8",
      maxBuffer: 10 * 1024 * 1024,
    });
    return { exitCode: 0, stdout: result.stdout, stderr: result.stderr };
  } catch (error: any) {
    return {
      exitCode: error.code === "ENOENT" ? 127 : Number.isInteger(error.code) ? error.code : 1,
      stdout: error.stdout ?? "",
      stderr: error.stderr ?? error.message ?? String(error),
    };
  }
};

const requireOk = async (file: string, args: string[], cwd = root) => {
  const result = await run(file, args, cwd);
  if (result.exitCode !== 0) {
    throw new Error(`${file} ${args.join(" ")} failed: ${result.stderr || result.stdout}`);
  }
  return result.stdout.trim();
};

const commandOk = async (command: string, cwd = root) => {
  const result = await run("/bin/sh", ["-c", command], cwd);
  return result;
};

async function resolveIssues() {
  const list = JSON.parse(await requireOk("gh", [
    "issue", "list", "--state", "all", "--limit", "100",
    "--json", "number,title,body,state,labels,url",
  ]));
  const byNumber = new Map(list.map((issue: any) => [issue.number, issue]));
  return list.map((issue: any) => ({
    ...issue,
    labels: issue.labels.map((label: any) => label.name),
    blockers: declaredBlockerNumbers(issue.body).map((number) => ({
      number,
      state: (byNumber.get(number) as any)?.state ?? "OPEN",
    })),
  }));
}

async function readProviderEnvName() {
  const configPath = join(process.env.CODEX_HOME || join(homedir(), ".codex"), "config.toml");
  return parseProviderEnvName(await readFile(configPath, "utf8"));
}

function fillTemplate(template: string, values: Record<string, string | number>) {
  return template.replace(/\{\{([A-Z_]+)\}\}/g, (_, key) => {
    if (!(key in values)) throw new Error(`missing template value ${key}`);
    return String(values[key]);
  });
}

async function createPane(cwd: string, env: Record<string, string>) {
  if (process.env.HERDR_ENV !== "1") throw new Error("workflow must run inside Herdr");
  const args = ["pane", "split", "--current", "--direction", "right", "--cwd", cwd];
  for (const [key, value] of Object.entries(env)) args.push("--env", `${key}=${value}`);
  args.push("--no-focus");
  const payload = JSON.parse(await requireOk("herdr", args));
  const paneId = payload?.result?.pane?.pane_id;
  if (!paneId) throw new Error("Herdr did not return the created pane ID");
  return paneId as string;
}

async function closePane(paneId: string) {
  await run("herdr", ["pane", "close", paneId]);
}

async function readPane(paneId: string) {
  const result = await run("herdr", [
    "pane", "read", paneId, "--source", "recent-unwrapped", "--lines", "300",
  ]);
  return result.stdout || result.stderr;
}

async function readPaneInfo(paneId: string) {
  const result = await run("herdr", ["pane", "get", paneId]);
  if (result.exitCode !== 0) return { error: result.stderr || result.stdout };
  try {
    return JSON.parse(result.stdout)?.result?.pane;
  } catch {
    return { error: "Herdr returned invalid pane metadata" };
  }
}

async function readPaneEvidence(paneId: string) {
  const paneInfo = await readPaneInfo(paneId);
  return `Herdr pane metadata:\n${JSON.stringify(paneInfo, null, 2)}\n\nRecent pane output:\n${await readPane(paneId)}`;
}

async function readRollouts(worktreePath: string, phaseStartedAt: string) {
  const rootDir = join(process.env.CODEX_HOME || join(homedir(), ".codex"), "sessions");
  const found: any[] = [];
  async function walk(directory: string) {
    for (const entry of await readdir(directory, { withFileTypes: true }).catch(() => [])) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        const info = await stat(path);
        if (info.mtimeMs < Date.parse(phaseStartedAt) - 60_000) continue;
        const first = (await readFile(path, "utf8")).split("\n")[0];
        try {
          const record = JSON.parse(first);
          if (record.type === "session_meta") {
            found.push({
              sessionId: record.payload.session_id || record.payload.id,
              cwd: record.payload.cwd,
              startedAt: record.payload.timestamp || record.timestamp,
              path,
            });
          }
        } catch { /* ignore unrelated or partial rollout files */ }
      }
    }
  }
  await walk(rootDir);
  return found.filter((item) => item.cwd === worktreePath);
}

async function readOptionalJson(path: string) {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch (error: any) {
    if (error.code === "ENOENT" || error instanceof SyntaxError) return undefined;
    throw error;
  }
}

async function runPhase({
  phase,
  worktreePath,
  promptPath,
  schemaPath,
  receiptPath,
  paneEvidencePath,
  providerEnvName,
}: {
  phase: "implementer" | "reviewer";
  worktreePath: string;
  promptPath: string;
  schemaPath: string;
  receiptPath: string;
  paneEvidencePath: string;
  providerEnvName: string;
}) {
  const credential = process.env[providerEnvName];
  if (!credential) throw new Error(`required provider variable ${providerEnvName} is absent`);
  const result = await runPhaseWithRetry({
    timeoutMs: options.timeoutMs,
    now: Date.now,
    sleep: (ms: number) => new Promise((resolve) => setTimeout(resolve, ms)),
    start: async (attempt: number) => {
      const phaseStartedAt = new Date().toISOString();
      // Isolate receipts and exit markers so a failed attempt cannot satisfy a retry.
      const attemptReceiptPath = `${receiptPath}.attempt-${attempt}`;
      const exitPath = `${attemptReceiptPath}.exit`;
      const paneId = await createPane(worktreePath, { [providerEnvName]: credential });
      const command = buildCodexPhaseCommand({
        model: options.model,
        effort: options.effort,
        worktreePath,
        schemaPath,
        receiptPath: attemptReceiptPath,
        promptPath,
      });
      try {
        // A dedicated shell writes the status only after Unsnooze has exited.
        // Do not infer exit from an idle/missing agent_session during startup.
        const trackedCommand = `${command}\nsandcastle_exit=$?\nprintf '%s\\n' "$sandcastle_exit" > ${shellQuote(exitPath)}`;
        await requireOk("herdr", ["pane", "run", paneId,
          `/bin/sh -c ${shellQuote(trackedCommand)}`]);
        return { paneId, phaseStartedAt, attemptReceiptPath, exitPath, attempt };
      } catch (error) {
        await closePane(paneId);
        throw error;
      }
    },
    inspect: async (worker: any) => {
      // Read exit first, then receipt: an exit marker guarantees receipt writes ended.
      const exitCode = await readOptionalJson(worker.exitPath);
      const receipt = await readOptionalJson(worker.attemptReceiptPath);
      return {
        receipt,
        exitCode,
        output: exitCode === undefined ? "" : await readPane(worker.paneId),
      };
    },
    validate: async (worker: any, receipt: any) => {
      const paneInfo = await readPaneInfo(worker.paneId);
      validateSessionEvidence({
        receiptSessionId: receipt.session_id,
        paneSession: paneInfo?.agent_session,
        paneUnsnoozeOwner: paneInfo?.tokens?.unsnooze_owner,
        rollouts: await readRollouts(worktreePath, worker.phaseStartedAt),
        worktreePath,
        phaseStartedAt: worker.phaseStartedAt,
      });
      validateFreshSessionId(receipt.session_id, usedSessionIds);
    },
    close: async (worker: any) => {
      try {
        const evidence = await readPaneEvidence(worker.paneId);
        await writeFile(`${paneEvidencePath}.attempt-${worker.attempt}`, evidence);
        await writeFile(paneEvidencePath, evidence);
      } finally {
        await closePane(worker.paneId);
      }
    },
  });
  await writeFile(receiptPath, `${JSON.stringify(result.receipt, null, 2)}\n`);
  return { receipt: result.receipt };
}

const issues = await resolveIssues();
const issue = selectReadyIssue(issues, options.issueOverride);
const baseSha = await requireOk("git", ["rev-parse", options.baseSha]);
const branch = options.branch || `sandcastle/issue-${issue.number}`;
const providerEnvName = await readProviderEnvName();
const requiredCommands = ["git", "gh", "herdr", "codex", "unsnooze", "python3"];
for (const command of requiredCommands) await requireOk("sh", ["-c", `command -v ${shellQuote(command)}`]);
if (!process.env[providerEnvName]) throw new Error(`required provider variable ${providerEnvName} is absent`);
for (const command of [options.focusedTest, options.finalTest]) {
  if (!command.trim()) throw new Error("test command is empty");
}

const plan = {
  issue: { number: issue.number, title: issue.title, url: issue.url },
  baseSha,
  branch,
  model: options.model,
  effort: options.effort,
  timeoutMs: options.timeoutMs,
  tests: { focused: options.focusedTest, final: options.finalTest },
  providerEnvName,
  phases: ["implementer", "reviewer"],
  githubMutation: false,
};

if (options.dryRun) {
  const artifacts = roundArtifactPaths("<artifacts>", 1);
  console.log(JSON.stringify({
    status: "preflight-ok",
    ...plan,
    plannedCommands: {
      implementer: buildCodexPhaseCommand({
        model: options.model,
        effort: options.effort,
        worktreePath: "<worktree>",
        schemaPath: artifacts.implementerSchemaPath,
        receiptPath: artifacts.implementerReceiptPath,
        promptPath: artifacts.implementerPromptPath,
      }),
      reviewer: buildCodexPhaseCommand({
        model: options.model,
        effort: options.effort,
        worktreePath: "<worktree>",
        schemaPath: artifacts.reviewerSchemaPath,
        receiptPath: artifacts.reviewerReceiptPath,
        promptPath: artifacts.reviewerPromptPath,
      }),
    },
  }, null, 2));
  process.exit(0);
}

await mkdir(artifactRoot, { recursive: true });
await writeFile(join(artifactRoot, "plan.json"), `${JSON.stringify(plan, null, 2)}\n`);

await using sandbox = await createSandbox({
  branch,
  baseBranch: baseSha,
  sandbox: noSandbox(),
  cwd: root,
});
const execInWorktree = async (command: string) => sandbox.exec(command);
const top = await execInWorktree("git rev-parse --show-toplevel");
if (top.exitCode !== 0) throw new Error(top.stderr || top.stdout);
const worktreePath = top.stdout.trim();
const controlDir = join(artifactRoot, "control");
await mkdir(controlDir, { recursive: true });

const templates = {
  implementer: await readFile(join(root, ".sandcastle", "implementer-prompt.md"), "utf8"),
  reviewer: await readFile(join(root, ".sandcastle", "reviewer-prompt.md"), "utf8"),
};
const usedSessionIds = new Set<string>();
const implementerSessionIds: string[] = [];
const reviewerSessionIds: string[] = [];
let round = 1;
let candidateHead = baseSha;
let reviewerFindings: string[] = [];
let focusedTestEvidence = "";

while (true) {
  const artifacts = roundArtifactPaths(artifactRoot, round);
  const previousCandidateHead = candidateHead;
  const implementerPrompt = fillTemplate(templates.implementer, {
    ISSUE_NUMBER: issue.number,
    ISSUE_TITLE: issue.title,
    ISSUE_BODY: issue.body,
    BASE_SHA: baseSha,
    BRANCH: branch,
    ROUND_CONTEXT: buildImplementerRoundContext({
      round,
      currentHead: candidateHead,
      reviewerFindings,
      focusedTestEvidence,
    }),
  });
  await writeFile(artifacts.implementerPromptPath, implementerPrompt);
  await copyFile(join(root, ".sandcastle", "implementer-schema.json"), artifacts.implementerSchemaPath);

  const implementer = await runPhase({
    phase: "implementer",
    worktreePath,
    promptPath: artifacts.implementerPromptPath,
    schemaPath: artifacts.implementerSchemaPath,
    receiptPath: artifacts.implementerReceiptPath,
    paneEvidencePath: artifacts.implementerPanePath,
    providerEnvName,
  });
  const implementationHeadResult = await execInWorktree("git rev-parse HEAD");
  if (implementationHeadResult.exitCode !== 0) throw new Error(implementationHeadResult.stderr);
  candidateHead = implementationHeadResult.stdout.trim();
  validateImplementerReceipt(implementer.receipt, { issueNumber: issue.number, head: candidateHead });
  validateFreshSessionId(implementer.receipt.session_id, usedSessionIds);
  usedSessionIds.add(implementer.receipt.session_id);
  implementerSessionIds.push(implementer.receipt.session_id);
  if (candidateHead === previousCandidateHead) throw new Error("implementer did not create a new candidate commit");

  const focused = await execInWorktree(options.focusedTest);
  focusedTestEvidence = `${focused.stdout}\n${focused.stderr}`;
  await writeFile(artifacts.focusedTestPath, focusedTestEvidence);
  if (focused.exitCode !== 0) throw new Error("focused implementation gate failed");

  const reviewerPrompt = fillTemplate(templates.reviewer, {
    ISSUE_NUMBER: issue.number,
    ISSUE_TITLE: issue.title,
    ISSUE_BODY: issue.body,
    BASE_SHA: baseSha,
    CANDIDATE_HEAD: candidateHead,
    TEST_EVIDENCE: focusedTestEvidence,
  });
  await writeFile(artifacts.reviewerPromptPath, reviewerPrompt);
  await copyFile(join(root, ".sandcastle", "reviewer-schema.json"), artifacts.reviewerSchemaPath);

  const reviewer = await runPhase({
    phase: "reviewer",
    worktreePath,
    promptPath: artifacts.reviewerPromptPath,
    schemaPath: artifacts.reviewerSchemaPath,
    receiptPath: artifacts.reviewerReceiptPath,
    paneEvidencePath: artifacts.reviewerPanePath,
    providerEnvName,
  });
  validateReviewerReceipt(reviewer.receipt, {
    reviewedHead: candidateHead,
    implementerSessionId: implementer.receipt.session_id,
  });
  validateFreshSessionId(reviewer.receipt.session_id, usedSessionIds);
  usedSessionIds.add(reviewer.receipt.session_id);
  reviewerSessionIds.push(reviewer.receipt.session_id);

  const headAfterReview = await execInWorktree("git rev-parse HEAD");
  if (headAfterReview.stdout.trim() !== candidateHead) throw new Error("reviewer changed Git HEAD");
  const dirtyAfterReview = await execInWorktree("git status --short --untracked-files=no");
  if (dirtyAfterReview.stdout.trim()) {
    throw new Error(`reviewer left tracked worktree changes:\n${dirtyAfterReview.stdout}`);
  }

  if (reviewer.receipt.verdict === "approved") break;
  if (reviewer.receipt.verdict === "blocked") {
    throw new Error(`reviewer verdict blocked: ${reviewer.receipt.findings.join("; ")}`);
  }
  reviewerFindings = reviewer.receipt.findings;
  round += 1;
}

const final = await execInWorktree(options.finalTest);
await writeFile(join(artifactRoot, "final-test.txt"), `${final.stdout}\n${final.stderr}`);
if (final.exitCode !== 0) throw new Error("final acceptance gate failed");
const dirty = await execInWorktree("git status --short --untracked-files=no");
if (dirty.stdout.trim()) throw new Error(`tracked worktree changes remain:\n${dirty.stdout}`);

const unsnoozeStatus = await commandOk("unsnooze status");
const warnings = [...implementerSessionIds, ...reviewerSessionIds]
  .filter((sessionId) => hasLingeringUnsnoozeState(unsnoozeStatus.stdout, sessionId))
  .map((sessionId) => `Unsnooze still tracks ${sessionId}; retained as cleanup evidence`);
await writeFile(join(artifactRoot, "result.json"), `${JSON.stringify({
  status: "reviewed-local-candidate",
  issue: issue.number,
  branch,
  baseSha,
  head: candidateHead,
  rounds: round,
  implementerSessionIds,
  reviewerSessionIds,
  verdict: "approved",
  warnings,
}, null, 2)}\n`);

console.log(JSON.stringify({
  status: "reviewed-local-candidate",
  issue: issue.number,
  branch,
  head: candidateHead,
  artifacts: artifactRoot,
  warnings,
}, null, 2));
