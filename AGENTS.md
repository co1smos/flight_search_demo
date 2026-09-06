## Agent skills

### Implementation workflow

- Use the official OpenAI Codex CLI as the default coding agent for implementation work in this repository.
- For bounded Sandcastle/Codex implementation runs, prefer `gpt-5.6-luna` with high reasoning effort when it is available to the authenticated Codex account; preflight the exact model before starting the real iteration and fall back to the account's verified default only if the preflight fails.
- Use Herdr to launch and coordinate Codex agents or subagents so their work is visible in separate tabs or panes.
- Ask the user before creating a new Herdr tab. Keep the current tab focused unless the user requests otherwise, and close agent tabs after their work is complete.
- Give each agent a narrow, self-contained task. Parallelize independent tasks, but avoid concurrent edits to the same files.
- Review each agent's changes and run the relevant tests, linters, or build before reporting completion.

### Issue tracker

Issues and specs are tracked in this repository’s GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The repository uses the five default canonical triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain-doc layout. See `docs/agents/domain.md`.
