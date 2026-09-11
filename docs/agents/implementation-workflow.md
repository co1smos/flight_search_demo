# Implementation workflow

Use this workflow for implementing a ready GitHub issue with Codex.

## Ownership

- Hermes resolves and dispatches one issue, monitors the visible Herdr panes, and owns any later GitHub/merge decision.
- Sandcastle is a deterministic Node controller. It creates one `noSandbox()` issue worktree, runs tests, validates receipts, and stops at a reviewed local branch.
- Codex implements or reviews one bounded phase. Implementer and reviewer are separate sessions.
- Unsnooze monitors and resumes Codex quota stops. The runner calls `unsnooze _run codex` explicitly; it does not depend on shell wrappers or a background daemon.
- Herdr provides visible panes. The controller closes only panes it creates.

## Token budget

Hermes applies the `AGENTS.md` routing rule to the resolved issue before preflight, then passes the selected tuple through `SANDCASTLE_MODEL` and `SANDCASTLE_EFFORT`. The runner has no hard-coded model default and fails closed when Hermes has not routed the work.

The initial workflow makes exactly two model calls per run using that routed tuple:

1. one implementer;
2. one fresh reviewer.

No automatic correction or re-review call is allowed. A failed gate or non-approved verdict stops for Hermes/human classification. This protects the shared ChatGPT Plus five-hour quota. Change this policy only after reviewing measured usage.

## Preflight

Run without a model call or repository mutation:

```sh
npm run sandcastle:preflight
```

The preflight verifies:

- required commands;
- one open, dependency-ready `ready-for-agent` issue (or `--issue N` override);
- exact base SHA and candidate branch;
- model, effort, two-call budget, timeout, and test commands;
- the active Codex provider's credential variable by name and presence only.

It never prints the credential value.

Useful overrides:

```sh
npm run sandcastle:preflight -- --issue 5
npm run sandcastle:preflight -- --issue 5 --branch sandcastle/issue-5
```

Environment options:

```text
SANDCASTLE_ISSUE
SANDCASTLE_BASE_SHA             default HEAD
SANDCASTLE_BRANCH               default sandcastle/issue-<number>
SANDCASTLE_MODEL                required; selected by Hermes from AGENTS.md
SANDCASTLE_EFFORT               required; paired with the routed model
SANDCASTLE_FOCUSED_TEST         default uv run --with pytest pytest -q
SANDCASTLE_FINAL_TEST           default uv run --with pytest pytest -q
SANDCASTLE_TIMEOUT_SECONDS      default 3600 per phase
SANDCASTLE_MAX_MODEL_CALLS      must be 2 initially
```

## Run

Start the deterministic controller in an ordinary visible Herdr pane:

```sh
npm run sandcastle:reviewed -- --issue 5
```

Hermes can trigger it by selecting the model/effort from `AGENTS.md`, then splitting a sibling shell pane and passing the exact tuple to the controller:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
herdr pane run <returned-pane-id> "SANDCASTLE_MODEL=gpt-5.6-sol SANDCASTLE_EFFORT=medium npm run sandcastle:reviewed -- --issue 5"
```

Use the pane ID returned by the split command. This controller pane is not a Codex session and consumes no model quota while it waits.

Do not start it through an outer Codex session. The controller launches the two model-backed panes itself.

Sequence:

```text
resolve/preflight
create noSandbox issue worktree
fresh Unsnooze-managed Codex implementer
validate exact session ID + rollout + committed HEAD
focused controller-owned tests
fresh Unsnooze-managed Codex reviewer
validate independent session + structured verdict + unchanged HEAD
final controller-owned tests
stop at local reviewed candidate branch
```

## Fail-closed rules

The workflow stops without advancing when:

- provider credential is absent;
- no ready unblocked issue exists;
- output is missing, malformed, or does not match the expected issue/head;
- exact session identity cannot be corroborated in pane and rollout evidence;
- a phase times out;
- implementer does not commit;
- either test gate fails;
- reviewer requests changes or is blocked;
- reviewer modifies Git HEAD or leaves tracked changes.

The runner never intentionally uses `codex resume --last`. Real Codex rollout IDs are required for validation. A lingering Unsnooze state after pane cleanup is retained as warning evidence, not treated as success.

## Safety

`noSandbox()` runs with the Ubuntu user's permissions. Use the issue worktree boundary, keep prompts free of secrets, and keep worker instructions read-only with respect to GitHub. Workers must not push, merge, close/comment/edit issues, or mutate shared lifecycle state.

The first integrated run stops locally. Inspect its branch and `.sandcastle/runs/<run-id>/` artifacts before authorizing PR publication or merge.
Prompts, schemas, receipts, pane evidence, and test logs live under that ignored artifact directory rather than inside the candidate worktree, so workers cannot accidentally commit controller files.

## Natural quota observation

Synthetic quota pause/resume is verified. A real quota boundary should be observed during normal work rather than manufactured by spending credits. When it happens, verify that Unsnooze binds recovery to the exact Codex session and that the phase still produces valid receipt/head evidence.
