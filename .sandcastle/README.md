# Sandcastle + Unsnooze workflow

This repository uses a deterministic Sandcastle controller with `noSandbox()`.
Hermes launches the controller in Herdr; the controller launches one fresh
Unsnooze-managed Codex implementer and one fresh reviewer in visible sibling
panes.

The initial workflow is deliberately capped at two model calls. It stops at a
local reviewed candidate branch and never pushes, merges, or mutates GitHub
issues.

Full policy and options:

```text
docs/agents/implementation-workflow.md
```

## Preflight: no model call

```sh
SANDCASTLE_MODEL=gpt-5.6-sol SANDCASTLE_EFFORT=medium npm run sandcastle:preflight
SANDCASTLE_MODEL=gpt-5.6-sol SANDCASTLE_EFFORT=medium npm run sandcastle:preflight -- --issue 5
```

Preflight resolves the issue/frontier and validates tools, branch/base, test
commands, provider credential presence, timeout, and the two-call budget. It
prints the credential variable name but never its value.

## Run one local reviewed candidate

Run from an ordinary Herdr shell pane:

```sh
SANDCASTLE_MODEL=gpt-5.6-sol SANDCASTLE_EFFORT=medium npm run sandcastle:reviewed -- --issue 5
```

From Hermes, use a normal sibling command pane—not another Codex agent:

```sh
herdr pane split --current --direction right --cwd "$PWD" --no-focus
herdr pane run <returned-pane-id> "SANDCASTLE_MODEL=gpt-5.6-sol SANDCASTLE_EFFORT=medium npm run sandcastle:reviewed -- --issue 5"
```

The controller then creates and closes the implementer/reviewer panes itself.

Defaults:

```text
model and effort: required; Hermes selects them from AGENTS.md
model calls: exactly 2
timeout: 3600 seconds per phase
focused test: uv run --with pytest pytest -q
final test: uv run --with pytest pytest -q
branch: sandcastle/issue-<number>
```

Override through documented `SANDCASTLE_*` environment variables or CLI options.
Do not run the controller through an outer Codex session.

## Unsnooze

Unsnooze is installed user-globally. The Sandcastle controller calls:

```text
unsnooze _run codex ...
```

explicitly, so shell wrappers and the GUI daemon are not required for this
workflow. Natural real-quota recovery remains a passive observation; do not burn
quota just to trigger it.

Inspect `.sandcastle/runs/<run-id>/` and the candidate branch after every run.
