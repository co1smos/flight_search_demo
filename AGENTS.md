## Agent skills

### Model routing

Use the cheapest model appropriate for the task. The default Codex session uses
`gpt-5.6-luna` with medium reasoning.

Handle simple tasks directly in the current Luna agent. Simple tasks are
mechanical edits, renames, formatting, boilerplate, straightforward tests,
obvious small fixes, and changes whose implementation is already explicit.

Use the `sol_worker` tier (`gpt-5.6-sol`, medium) for normal engineering work:
features with clear requirements, normal bug fixes, related multi-file changes,
business logic, normal code review, and refactoring that requires understanding
existing code.

Use the `astra_worker` tier (`gpt-6-astra`, low) for hard work: unclear root
causes, difficult debugging, unfamiliar architecture, concurrency or distributed
systems, large cross-component changes, ambiguous requirements, subtle
correctness problems, or a failed Sol attempt.

Do not escalate merely to improve confidence, and do not spawn agents only to
classify a task. Escalate Luna to Sol when substantial reasoning beyond
mechanical execution is required. Escalate Sol to Astra only when the problem
remains unresolved, important assumptions cannot be established, verification
fails, or the task clearly belongs in the hard category.

Before launching a delegated worker, preflight its exact model and reasoning
effort. If that tier is unavailable, preflight the configured Gemini 3.8 model
ID and use it with provider-default reasoning. If Gemini 3.8 is unavailable or
not configured, use the authenticated Codex account's verified default and
report the fallback.

### Implementation workflow

- Use the official OpenAI Codex CLI as the default coding agent for implementation work in this repository.
- Apply the same model routing to direct Codex work and bounded Sandcastle/Codex iterations.
- When asked to implement a GitHub issue with Codex or Sandcastle—including a concise request such as `Implement issue #5`—load and follow `docs/agents/implementation-workflow.md`. Hermes launches the deterministic Sandcastle controller; Sandcastle owns the no-sandbox issue worktree and test gates; each Codex phase runs visibly through Unsnooze in a fresh Herdr pane. The user need not provide shell commands.
- Before Sandcastle preflight, Hermes routes the issue using the model rules above and passes the exact model/effort tuple to the controller. Initial runs are capped at two calls with that tuple: one implementer and one independent reviewer. A failed gate stops rather than launching an automatic correction loop.
- Keep every delegated worker visible in a separate Herdr pane or tab. Do not use Codex's hidden native subagent spawning for repository work, even though `.codex/agents/*.toml` records the tier definitions.
- Ask the user before creating a new Herdr tab. A sibling pane in the current tab is the default and does not require another prompt. Keep the current tab focused unless the user requests otherwise, and close panes or tabs created for workers after their work is complete.
- Give each worker a narrow, self-contained task and launch it with the model and reasoning values recorded in the matching `.codex/agents/*.toml` file. Parallelize independent tasks, but avoid concurrent edits to the same files.
- Review each worker's changes and run the relevant tests, linters, or build before reporting completion.

### Issue tracker

Issues and specs are tracked in this repository’s GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The repository uses the five default canonical triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain-doc layout. See `docs/agents/domain.md`.
