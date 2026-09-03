# Sandcastle workflow

This repository uses Sandcastle's `simple-loop` template with Codex and
`noSandbox()`. Each invocation selects one open `ready-for-agent` issue whose
blockers are closed and works on the explicit `sandcastle/worker` branch.

## Before the first run

1. Authenticate Codex on the host with `codex login`.
2. Export a GitHub token for the agent process, for example:
   `export GH_TOKEN="$(gh auth token)"`.
3. Approve and publish the tracer-bullet tickets. Do not start the worker while
   parent PRD #1 is the only `ready-for-agent` issue.

## Run one ticket

```sh
npm run sandcastle
```

The run is intentionally limited to one iteration. Inspect the resulting
`sandcastle/worker` branch and `.sandcastle/logs/` before merging or starting
the next ticket.

## Safety

`noSandbox()` provides no container boundary. Codex runs with this user's host
permissions. Keep the one-ticket limit, review every branch, and do not put
secrets in the repository or prompts.