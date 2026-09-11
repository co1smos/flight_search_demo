import { createSandbox } from "@ai-hero/sandcastle";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import { execFile } from "node:child_process";
import { copyFile, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import {
  buildCodexPhaseCommand,
  declaredBlockerNumbers,
  hasLingeringUnsnoozeState,
  parseCliOptions,
  parseProviderEnvName,
  selectReadyIssue,
  shellQuote,
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

async function waitForJson(path: string, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      return JSON.parse(await readFile(path, "utf8"));
    } catch (error: any) {
      if (error.code !== "ENOENT" && !(error instanceof SyntaxError)) throw error;
    }
    await new Promise((resolveTimeout) => setTimeout(resolveTimeout, 500));
  }
  throw new Error(`timed out waiting for ${path}`);
}

async function runPhase({
  phase,
  worktreePath,
  promptPath,
  schemaPath,
  receiptPath,
  providerEnvName,
}: {
  phase: "implementer" | "reviewer";
  worktreePath: string;
  promptPath: string;
  schemaPath: string;
  receiptPath: string;
  providerEnvName: string;
}) {
  const credential = process.env[providerEnvName];
  if (!credential) throw new Error(`required provider variable ${providerEnvName} is absent`);
  const phaseStartedAt = new Date().toISOString();
  const paneId = await createPane(worktreePath, { [providerEnvName]: credential });
  const command = buildCodexPhaseCommand({
    model: options.model,
    effort: options.effort,
    worktreePath,
    schemaPath,
    receiptPath,
    promptPath,
  });
  try {
    await requireOk("herdr", ["pane", "run", paneId, command]);
    const receipt = await waitForJson(receiptPath, options.timeoutMs);
    const paneText = await readPane(paneId);
    validateSessionEvidence({
      receiptSessionId: receipt.session_id,
      paneText,
      rollouts: await readRollouts(worktreePath, phaseStartedAt),
      worktreePath,
      phaseStartedAt,
    });
    return { receipt, paneId };
  } catch (error) {
    await writeFile(join(artifactRoot, `${phase}-pane.txt`), await readPane(paneId));
    throw error;
  } finally {
    await closePane(paneId);
  }
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
  maxModelCalls: options.maxModelCalls,
  timeoutMs: options.timeoutMs,
  tests: { focused: options.focusedTest, final: options.finalTest },
  providerEnvName,
  phases: ["implementer", "reviewer"],
  githubMutation: false,
};

if (options.dryRun) {
  const controlDir = join("<artifacts>", "control");
  console.log(JSON.stringify({
    status: "preflight-ok",
    ...plan,
    plannedCommands: {
      implementer: buildCodexPhaseCommand({
        model: options.model,
        effort: options.effort,
        worktreePath: "<worktree>",
        schemaPath: join(controlDir, "implementer-schema.json"),
        receiptPath: join("<artifacts>", "implementer.json"),
        promptPath: join(controlDir, "implementer.md"),
      }),
      reviewer: buildCodexPhaseCommand({
        model: options.model,
        effort: options.effort,
        worktreePath: "<worktree>",
        schemaPath: join(controlDir, "reviewer-schema.json"),
        receiptPath: join("<artifacts>", "reviewer.json"),
        promptPath: join(controlDir, "reviewer.md"),
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
const implementerPrompt = fillTemplate(templates.implementer, {
  ISSUE_NUMBER: issue.number,
  ISSUE_TITLE: issue.title,
  ISSUE_BODY: issue.body,
  BASE_SHA: baseSha,
  BRANCH: branch,
});
const implementerPromptPath = join(controlDir, "implementer.md");
const implementerSchemaPath = join(controlDir, "implementer-schema.json");
const implementerReceiptPath = join(artifactRoot, "implementer.json");
await writeFile(implementerPromptPath, implementerPrompt);
await copyFile(join(root, ".sandcastle", "implementer-schema.json"), implementerSchemaPath);

const implementer = await runPhase({
  phase: "implementer",
  worktreePath,
  promptPath: implementerPromptPath,
  schemaPath: implementerSchemaPath,
  receiptPath: implementerReceiptPath,
  providerEnvName,
});
const implementationHeadResult = await execInWorktree("git rev-parse HEAD");
if (implementationHeadResult.exitCode !== 0) throw new Error(implementationHeadResult.stderr);
const implementationHead = implementationHeadResult.stdout.trim();
validateImplementerReceipt(implementer.receipt, { issueNumber: issue.number, head: implementationHead });
if (implementationHead === baseSha) throw new Error("implementer did not create a candidate commit");

const focused = await execInWorktree(options.focusedTest);
await writeFile(join(artifactRoot, "focused-test.txt"), `${focused.stdout}\n${focused.stderr}`);
if (focused.exitCode !== 0) throw new Error("focused implementation gate failed");

const reviewerPrompt = fillTemplate(templates.reviewer, {
  ISSUE_NUMBER: issue.number,
  ISSUE_TITLE: issue.title,
  ISSUE_BODY: issue.body,
  BASE_SHA: baseSha,
  CANDIDATE_HEAD: implementationHead,
  TEST_EVIDENCE: focused.stdout.slice(-6000),
});
const reviewerPromptPath = join(controlDir, "reviewer.md");
const reviewerSchemaPath = join(controlDir, "reviewer-schema.json");
const reviewerReceiptPath = join(artifactRoot, "reviewer.json");
await writeFile(reviewerPromptPath, reviewerPrompt);
await copyFile(join(root, ".sandcastle", "reviewer-schema.json"), reviewerSchemaPath);

const reviewer = await runPhase({
  phase: "reviewer",
  worktreePath,
  promptPath: reviewerPromptPath,
  schemaPath: reviewerSchemaPath,
  receiptPath: reviewerReceiptPath,
  providerEnvName,
});
validateReviewerReceipt(reviewer.receipt, {
  reviewedHead: implementationHead,
  implementerSessionId: implementer.receipt.session_id,
});
if (reviewer.receipt.verdict !== "approved") {
  throw new Error(`reviewer verdict ${reviewer.receipt.verdict}: ${reviewer.receipt.findings.join("; ")}`);
}

const headAfterReview = await execInWorktree("git rev-parse HEAD");
if (headAfterReview.stdout.trim() !== implementationHead) throw new Error("reviewer changed Git HEAD");
const final = await execInWorktree(options.finalTest);
await writeFile(join(artifactRoot, "final-test.txt"), `${final.stdout}\n${final.stderr}`);
if (final.exitCode !== 0) throw new Error("final acceptance gate failed");
const dirty = await execInWorktree("git status --short --untracked-files=no");
if (dirty.stdout.trim()) throw new Error(`tracked worktree changes remain:\n${dirty.stdout}`);

const unsnoozeStatus = await commandOk("unsnooze status");
const warnings = [implementer.receipt.session_id, reviewer.receipt.session_id]
  .filter((sessionId) => hasLingeringUnsnoozeState(unsnoozeStatus.stdout, sessionId))
  .map((sessionId) => `Unsnooze still tracks ${sessionId}; retained as cleanup evidence`);
await writeFile(join(artifactRoot, "result.json"), `${JSON.stringify({
  status: "reviewed-local-candidate",
  issue: issue.number,
  branch,
  baseSha,
  head: implementationHead,
  implementerSessionId: implementer.receipt.session_id,
  reviewerSessionId: reviewer.receipt.session_id,
  verdict: reviewer.receipt.verdict,
  warnings,
}, null, 2)}\n`);

console.log(JSON.stringify({
  status: "reviewed-local-candidate",
  issue: issue.number,
  branch,
  head: implementationHead,
  artifacts: artifactRoot,
  warnings,
}, null, 2));
