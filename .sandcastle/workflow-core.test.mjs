import assert from "node:assert/strict";
import test from "node:test";

import {
  buildImplementerRoundContext,
  buildCodexPhaseCommand,
  declaredBlockerNumbers,
  hasLingeringUnsnoozeState,
  parseCliOptions,
  parseProviderEnvName,
  roundArtifactPaths,
  selectReadyIssue,
  shellQuote,
  validateFreshSessionId,
  validateImplementerReceipt,
  validateReviewerReceipt,
  validateSessionEvidence,
} from "./workflow-core.mjs";

test("parseCliOptions applies safe defaults and environment overrides", () => {
  assert.throws(
    () => parseCliOptions([], {}),
    /model must be explicitly routed/,
  );

  assert.deepEqual(parseCliOptions([], {
    SANDCASTLE_MODEL: "gpt-5.6-luna",
    SANDCASTLE_EFFORT: "medium",
  }), {
    issueOverride: undefined,
    baseSha: "HEAD",
    branch: undefined,
    model: "gpt-5.6-luna",
    effort: "medium",
    focusedTest: "uv run --with pytest pytest -q",
    finalTest: "uv run --with pytest pytest -q",
    timeoutMs: 3_600_000,
    dryRun: false,
  });

  assert.deepEqual(
    parseCliOptions(
      ["--issue", "42", "--branch", "sandcastle/issue-42", "--preflight"],
      {
        SANDCASTLE_BASE_SHA: "abc123",
        SANDCASTLE_MODEL: "gpt-5.6-sol",
        SANDCASTLE_EFFORT: "high",
        SANDCASTLE_FOCUSED_TEST: "python -m pytest -q tests/test_issue_42.py",
        SANDCASTLE_FINAL_TEST: "python -m pytest -q && python -m compileall -q src",
        SANDCASTLE_TIMEOUT_SECONDS: "90",
      },
    ),
    {
      issueOverride: 42,
      baseSha: "abc123",
      branch: "sandcastle/issue-42",
      model: "gpt-5.6-sol",
      effort: "high",
      focusedTest: "python -m pytest -q tests/test_issue_42.py",
      finalTest: "python -m pytest -q && python -m compileall -q src",
      timeoutMs: 90_000,
      dryRun: true,
    },
  );
});

test("SANDCASTLE_MAX_MODEL_CALLS is not a supported parsing control", () => {
  const routed = {
    SANDCASTLE_MODEL: "gpt-5.6-luna",
    SANDCASTLE_EFFORT: "medium",
  };
  const options = parseCliOptions([], {
    ...routed,
    SANDCASTLE_MAX_MODEL_CALLS: "3",
  });

  assert.equal(Object.hasOwn(options, "maxModelCalls"), false);
  assert.deepEqual(options, parseCliOptions([], routed));
});

test("parseCliOptions rejects unsafe or ambiguous values", () => {
  const routed = { SANDCASTLE_MODEL: "gpt-5.6-luna", SANDCASTLE_EFFORT: "medium" };
  assert.throws(() => parseCliOptions(["--issue", "0"], routed), /positive integer/);
  assert.throws(() => parseCliOptions(["--timeout", "nope"], routed), /timeout/);
  assert.throws(() => parseCliOptions(["--wat"], routed), /unknown option/);
  assert.throws(
    () => parseCliOptions(["--branch", "main; rm -rf x"], routed),
    /branch/,
  );
});

test("parseProviderEnvName reads the configured Codex provider credential name", () => {
  const config = `
model_provider = "headroom"

[model_providers.headroom]
name = "CLIProxyAPI via Headroom"
env_key = "HERMES_CUSTOM_127_0_0_1_8317_API_KEY"
`;
  assert.equal(
    parseProviderEnvName(config),
    "HERMES_CUSTOM_127_0_0_1_8317_API_KEY",
  );
  assert.throws(() => parseProviderEnvName("model = \"gpt-5.6-luna\""), /model_provider/);
});

test("declaredBlockerNumbers extracts and de-duplicates fallback blocker declarations", () => {
  assert.deepEqual(
    declaredBlockerNumbers("Blocked by: #9, #3\nDepends on #9\nUnrelated #44"),
    [3, 9],
  );
});

test("selectReadyIssue uses the lowest-numbered unblocked ready issue", () => {
  const issues = [
    {
      number: 8,
      state: "OPEN",
      labels: ["ready-for-agent"],
      blockers: [],
    },
    {
      number: 3,
      state: "OPEN",
      labels: ["ready-for-agent"],
      blockers: [{ number: 1, state: "OPEN" }],
    },
    {
      number: 5,
      state: "OPEN",
      labels: ["ready-for-agent"],
      blockers: [{ number: 2, state: "CLOSED" }],
    },
  ];
  assert.equal(selectReadyIssue(issues).number, 5);
  assert.equal(selectReadyIssue(issues, 8).number, 8);
  assert.throws(() => selectReadyIssue(issues, 3), /open blocker #1/);
  assert.throws(() => selectReadyIssue(issues, 99), /override issue #99/);
});

test("shellQuote and buildCodexPhaseCommand produce one direct Unsnooze command", () => {
  assert.equal(shellQuote("plain-value"), "plain-value");
  assert.equal(shellQuote("it's spaced"), `'it'"'"'s spaced'`);

  const command = buildCodexPhaseCommand({
    model: "gpt-5.6-sol",
    effort: "medium",
    worktreePath: "/tmp/work tree",
    schemaPath: "/tmp/run/implementer-schema.json",
    receiptPath: "/tmp/run/implementer.json",
    promptPath: "/tmp/run/implementer prompt.md",
  });

  assert.equal(
    command,
    "unsnooze _run codex --ask-for-approval never --no-alt-screen exec --model gpt-5.6-sol --config 'model_reasoning_effort=\"medium\"' --sandbox danger-full-access --color never --cd '/tmp/work tree' --output-schema /tmp/run/implementer-schema.json --output-last-message /tmp/run/implementer.json - < '/tmp/run/implementer prompt.md'",
  );
  assert.doesNotMatch(command, /resume|--last/);
});

test("validateImplementerReceipt requires the expected issue, session, and Git head", () => {
  const receipt = {
    phase: "implementer",
    status: "completed",
    issue_number: 42,
    session_id: "01a08d05-d511-7582-bcff-2c26d5ae3c5a",
    head: "a".repeat(40),
    completed_at: "2026-09-11T00:00:00.000Z",
  };
  assert.deepEqual(
    validateImplementerReceipt(receipt, {
      issueNumber: 42,
      head: "a".repeat(40),
    }),
    receipt,
  );
  assert.throws(
    () => validateImplementerReceipt({ ...receipt, head: "b".repeat(40) }, {
      issueNumber: 42,
      head: "a".repeat(40),
    }),
    /head/,
  );
});

test("validateReviewerReceipt enforces a fresh session and verdict contract", () => {
  const receipt = {
    phase: "reviewer",
    status: "completed",
    verdict: "approved",
    session_id: "01a08da0-ad62-75d2-b850-b0cbf944a1c9",
    reviewed_head: "a".repeat(40),
    completed_at: "2026-09-11T00:02:00.000Z",
    findings: [],
  };
  assert.deepEqual(
    validateReviewerReceipt(receipt, {
      reviewedHead: "a".repeat(40),
      implementerSessionId: "01a08d05-d511-7582-bcff-2c26d5ae3c5a",
    }),
    receipt,
  );
  assert.throws(
    () => validateReviewerReceipt({ ...receipt, findings: ["missing test"] }, {
      reviewedHead: "a".repeat(40),
      implementerSessionId: "01a08d05-d511-7582-bcff-2c26d5ae3c5a",
    }),
    /approved.*findings/,
  );
  assert.throws(
    () => validateReviewerReceipt({ ...receipt, session_id: "01a08d05-d511-7582-bcff-2c26d5ae3c5a" }, {
      reviewedHead: "a".repeat(40),
      implementerSessionId: "01a08d05-d511-7582-bcff-2c26d5ae3c5a",
    }),
    /fresh session/,
  );
});

test("round artifacts are unique across correction rounds", () => {
  assert.deepEqual(roundArtifactPaths("/tmp/run", 1), {
    implementerPromptPath: "/tmp/run/control/round-1-implementer.md",
    implementerSchemaPath: "/tmp/run/control/round-1-implementer-schema.json",
    implementerReceiptPath: "/tmp/run/round-1-implementer.json",
    implementerPanePath: "/tmp/run/round-1-implementer-pane.txt",
    focusedTestPath: "/tmp/run/round-1-focused-test.txt",
    reviewerPromptPath: "/tmp/run/control/round-1-reviewer.md",
    reviewerSchemaPath: "/tmp/run/control/round-1-reviewer-schema.json",
    reviewerReceiptPath: "/tmp/run/round-1-reviewer.json",
    reviewerPanePath: "/tmp/run/round-1-reviewer-pane.txt",
  });
  assert.notDeepEqual(
    roundArtifactPaths("/tmp/run", 1),
    roundArtifactPaths("/tmp/run", 2),
  );
});

test("correction implementer context includes exact prior review and test evidence", () => {
  assert.equal(buildImplementerRoundContext({
    round: 2,
    currentHead: "a".repeat(40),
    reviewerFindings: ["Add the missing airport-code assertion.", "Keep the existing fallback."],
    focusedTestEvidence: "12 passing\n",
  }), `This is correction round 2.
Current candidate HEAD: ${"a".repeat(40)}

Exact reviewer findings from the previous round:
["Add the missing airport-code assertion.","Keep the existing fallback."]

Previous controller-owned focused-test evidence:
12 passing

You must correct these findings and create a new commit on top of the current candidate HEAD.`);
});

test("session IDs cannot be reused from any earlier phase in the run", () => {
  const firstImplementer = "01a08d05-d511-7582-bcff-2c26d5ae3c5a";
  const firstReviewer = "01a08da0-ad62-75d2-b850-b0cbf944a1c9";
  const secondImplementer = "01a08e10-468d-7c87-a4ea-a1d405b46554";
  const used = new Set([firstImplementer, firstReviewer]);

  assert.equal(validateFreshSessionId(secondImplementer, used), secondImplementer);
  assert.throws(() => validateFreshSessionId(firstImplementer, used), /prior session/);
  assert.throws(() => validateFreshSessionId(firstReviewer, used), /prior session/);
});

test("validateSessionEvidence requires exact pane and rollout agreement", () => {
  const sessionId = "01a08d05-d511-7582-bcff-2c26d5ae3c5a";
  const evidence = validateSessionEvidence({
    receiptSessionId: sessionId,
    paneSession: {
      agent: "codex",
      kind: "id",
      source: "herdr:codex",
      value: sessionId,
    },
    rollouts: [
      {
        sessionId,
        cwd: "/tmp/worktree",
        startedAt: "2026-09-11T00:00:01.000Z",
        path: "/home/ubuntu/.codex/sessions/rollout.jsonl",
      },
    ],
    worktreePath: "/tmp/worktree",
    phaseStartedAt: "2026-09-11T00:00:00.000Z",
  });
  assert.equal(evidence.rolloutPath, "/home/ubuntu/.codex/sessions/rollout.jsonl");

  assert.throws(
    () => validateSessionEvidence({
      receiptSessionId: sessionId,
      paneSession: undefined,
      rollouts: [],
      worktreePath: "/tmp/worktree",
      phaseStartedAt: "2026-09-11T00:00:00.000Z",
    }),
    /pane evidence/,
  );
  assert.throws(
    () => validateSessionEvidence({
      receiptSessionId: sessionId,
      paneSession: {
        agent: "codex",
        kind: "id",
        source: "herdr:codex",
        value: "01a08da0-ad62-75d2-b850-b0cbf944a1c9",
      },
      rollouts: [],
      worktreePath: "/tmp/worktree",
      phaseStartedAt: "2026-09-11T00:00:00.000Z",
    }),
    /pane evidence/,
  );
});

test("validateSessionEvidence accepts Herdr pane identity after long output evicts the session header", () => {
  const sessionId = "01a08ef7-dc47-7341-9bd0-ba1569b4a2f7";
  const evidence = validateSessionEvidence({
    receiptSessionId: sessionId,
    paneText: "the last 300 lines contain only the end of a very large diff",
    paneSession: {
      agent: "codex",
      kind: "id",
      source: "herdr:codex",
      value: sessionId,
    },
    rollouts: [
      {
        sessionId,
        cwd: "/tmp/worktree",
        startedAt: "2026-09-11T05:30:01.000Z",
        path: "/home/ubuntu/.codex/sessions/rollout.jsonl",
      },
    ],
    worktreePath: "/tmp/worktree",
    phaseStartedAt: "2026-09-11T05:30:00.000Z",
  });

  assert.equal(evidence.rolloutPath, "/home/ubuntu/.codex/sessions/rollout.jsonl");
});

test("lingering Unsnooze resuming state is warning evidence", () => {
  const sessionId = "01a08d05-d511-7582-bcff-2c26d5ae3c5a";
  assert.equal(
    hasLingeringUnsnoozeState(`[RESUMING] codex ${sessionId}`, sessionId),
    true,
  );
  assert.equal(hasLingeringUnsnoozeState("no tracked sessions", sessionId), false);
});
