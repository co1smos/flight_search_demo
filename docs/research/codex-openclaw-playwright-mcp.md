# Codex, OpenClaw, and Playwright MCP for reusable browser automation

Research date: 2026-09-03. Sources are limited to first-party documentation and source repositories. Repository observations are pinned to the inspected commits:

- OpenAI Codex: [`ec84e692`](https://github.com/openai/codex/tree/ec84e692611d50d9c79c4bdad0a5785013975a72)
- OpenClaw: [`84bbb1db`](https://github.com/openclaw/openclaw/tree/84bbb1db5d2aa9bea8f37695a690f16e655b3a45)
- Microsoft Playwright MCP: [`4c1fb03b`](https://github.com/microsoft/playwright-mcp/tree/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91)

## Executive conclusion

For a standalone Python flight-search service orchestrated by Gemini, **Playwright MCP is the simplest reusable off-the-shelf browser tool server of the three**. It is explicitly an MCP server for any MCP client, supports stdio and standalone HTTP transport, runs headless (including an official headless-Chromium Docker image), provides persistent profiles and isolated storage-state sessions, and exposes accessibility snapshots whose element references can be passed directly to click/fill/type tools. It also has a JavaScript embedding API, although the cleanest Python integration is to use an MCP client over stdio or HTTP rather than trying to import its Node library directly.[^pwmcp-purpose] [^pwmcp-standalone] [^pwmcp-programmatic]

**OpenClaw's browser is technically scriptable outside an agent turn**, via its CLI and an opt-in authenticated loopback HTTP control API. It has excellent persistent-profile, session/tab ownership, snapshot/ref, headless-Linux, remote-CDP, and operator-UI capabilities. However, that API and service are a bundled OpenClaw Gateway plugin rather than a separately packaged general-purpose MCP server or Python library. Reusing it therefore means operating OpenClaw/Gateway as browser infrastructure and coupling the flight application to OpenClaw's profile, routing, auth, session, and policy model. That is viable, but not the simplicity-first choice for an otherwise standalone Gemini application.[^oc-plugin] [^oc-control-api] [^oc-internal]

**Codex is not a browser automation library/tool server to reuse.** The open-source Codex runtime is a coding-agent harness and MCP client. Its browser/computer-use surfaces are plugin/app integrations routed through Codex (`node_repl`, `cua_repl`, and an OpenAI browser connector), with Codex-specific policy metadata, approvals, hooks, and thread lifecycle. The repository exposes a Codex Python SDK and app-server, but those embed/control Codex threads and turns; they do not publish the browser/computer implementation as an independent browser MCP server or Python API. If using Codex, the documented practical browser path is to attach an external server such as Playwright MCP to Codex—not to extract a Codex browser backend.[^codex-app-server] [^codex-sdk] [^codex-plugin-ids] [^codex-browser-routing] [^pwmcp-codex]

## Capability matrix

| Capability | OpenAI Codex browser/computer use | OpenClaw browser | Playwright MCP |
|---|---|---|---|
| Reusable in standalone Python + Gemini | **No independent reusable browser package found.** Browser/computer use is integrated into Codex plugins/apps and MCP actor routes. One could embed the entire Codex agent through its Python SDK/app-server, but then Codex—not Gemini alone—is the harness. | **Possible through a service boundary**, not as a small library: run OpenClaw Gateway and call its authenticated loopback HTTP API or CLI. This brings substantial harness/runtime coupling. | **Yes. Recommended.** It is an MCP server intended for “any other MCP client”; a Python MCP client can spawn it over stdio or call a standalone HTTP endpoint. |
| Headless text-only Ubuntu VPS | No independently documented Codex browser server/VPS launch contract to verify. Computer-use config in source is explicitly macOS/Windows app-oriented; browser execution is supplied by external Codex integrations. | **Yes.** Managed local profiles automatically default to headless on Linux when neither `DISPLAY` nor `WAYLAND_DISPLAY` exists. Headless can also be selected globally, per profile, by environment variable, or per start request. | **Yes.** `--headless` is supported. Official Docker supports headless Chromium and can run as a long-lived MCP HTTP service with `--headless --no-sandbox --port ... --host 0.0.0.0`. |
| Persistent login/profile | Not exposed/documented as a standalone Codex-owned browser profile facility in the OSS browser implementation (which is not present as an independent package). | **Yes.** Managed profiles have dedicated user-data directories. Multiple named profiles, cookies/local/session storage APIs, and remote/existing signed-in browser profiles are supported. | **Yes.** Persistent profile is the default operating mode; `--user-data-dir` selects it. Logged-in data survives sessions. `--isolated --storage-state=...` is the alternative. |
| Accessibility snapshot + element-ref actions | The inspected OSS harness contains browser policy and connector plumbing, but no stable public, standalone browser tool contract to adopt. | **Yes.** Snapshot returns AI/ARIA trees and refs; `act` uses those refs for click/type/drag/select. Refs are scoped to the latest snapshot and tab/target. | **Yes.** Structured accessibility snapshots are the core design. Action tools accept the exact `target` ref from a page snapshot (or a unique selector). Screenshots are explicitly not the action basis. |
| Browser/session model | Codex has persistent agent **threads/turns**, but those are Codex conversational sessions, not a documented reusable browser-context/session API. Browser cleanup is attached to Codex stop/subagent-stop hooks. | **Yes.** Named browser profiles; stable tab IDs/labels; session-owned tabs; durable ownership for qualifying CDP targets across Gateway restarts; explicit cleanup on session lifecycle events. | **Yes.** Persistent profile, isolated per-session contexts, optional storage-state bootstrap, and `--shared-browser-context` for reuse across connected HTTP clients. A persistent profile can be held by only one browser instance at a time. |
| Human takeover / intervention | No first-party standalone browser takeover facility was found that a Gemini application could call. Any UI/approval behavior is part of Codex clients and plugin integrations. | **Strongest integrated operator story**, but OpenClaw-specific. The Control UI has an interactive Browser panel that follows the session's selected tab and can navigate/click/type/scroll/inspect/annotate. The browser skill reports login/2FA/CAPTCHA/native permission blockers for manual action. It can also attach to a person's signed-in Chrome; Chrome MCP attachment needs a person present for initial consent, while OpenClaw's extension mode can operate while nobody is at the desk. | **Partial, not a hosted takeover console.** Extension mode connects to existing Chrome/Edge tabs and their logged-in state, so the human and agent can use the same visible browser. DevTools capability can record actions the user performs and has an annotation dashboard. On a headless VPS, Playwright MCP itself does not supply VNC/noVNC or a remote human desktop; add a display/remote-desktop layer, or use extension/CDP to a browser on a human-accessible machine. |
| Harness coupling | **High / intrinsic.** | **Medium-high.** API exists, but it is one unit with the OpenClaw browser plugin/control service/Gateway. | **Low.** Standard MCP boundary; model- and harness-agnostic. |

## 1. OpenAI Codex

### What Codex provides

Codex itself is an agent runtime. Its app-server is the JSON-RPC interface used to power rich Codex clients; its primitives are Codex threads, turns, items, approvals, and tool events.[^codex-app-server] The Python SDK similarly says it builds applications that “start Codex threads, run turns, stream progress, and control workspace access,” and installs/reuses a matching Codex CLI runtime.[^codex-sdk] This is an SDK for embedding the **Codex agent**, not a browser-driver SDK.

Current Codex source includes stable feature flags and policy configuration for browser and computer use. Browser policy covers origin access, downloads, uploads, history, and full CDP access; computer-use policy covers default application access and macOS bundle IDs / Windows AUMIDs and executables.[^codex-features] [^codex-browser-config] [^codex-computer-config] This confirms that Codex has first-class governance around these capabilities, but does not establish a reusable browser implementation.

The implementation boundary is the important part:

- Codex's discoverable plugins include `chrome@openai-bundled` and `computer-use@openai-bundled`.[^codex-plugin-ids]
- Browser/computer actor calls are recognized as the MCP servers `node_repl` and `cua_repl`, and Codex injects Codex model confirmation-policy metadata only into those routes.[^codex-policy-routing]
- Browser cleanup tests route `browser.turn_ended` to an OpenAI app connector named `connector_openai_browser`; computer-use cleanup is a separate bundled plugin hook.[^codex-browser-routing]

Thus the open-source harness contains integration, policy, lifecycle, approval, and connector plumbing. The actual Browser app/REPL environment is not exposed in this repository as a standalone installable browser server with a public launch/config/profile contract comparable to Playwright MCP.

### Reuse verdict

- **Do not plan to import “Codex browser tools” into the Gemini flight application.** There is no official standalone Python library or generally documented server endpoint for that browser implementation.
- Running the Codex Python SDK/app-server would embed Codex as another agent/harness. It might be useful if the product intentionally delegates entire tasks to Codex, but it is not a simple way to give Gemini click/fill/inspect primitives.
- Codex is, however, an MCP client. Playwright MCP's official README documents adding the Playwright server to Codex. That reinforces the architecture: browser automation is supplied to Codex by a reusable external MCP server.[^pwmcp-codex]

### Requested feature verification

- **Headless VPS:** not verifiable as a standalone Codex browser capability from official OSS docs/source. The standalone computer-use policy is macOS/Windows-app shaped; browser execution is external/plugin-backed.
- **Persistent profiles:** no independent Codex browser-profile API/package found.
- **Snapshot/element refs:** no public independent Codex browser server contract found in the repository.
- **Sessions:** Codex threads persist and resume, but this is agent history, not a browser context API.[^codex-app-server]
- **Human takeover:** Codex app clients can handle approvals and rich UI events, but no reusable browser takeover console/server for a Gemini application is documented.

## 2. OpenClaw

### Architecture and external accessibility

OpenClaw's default browser is a **bundled plugin**. It launches a dedicated Chromium profile and runs a local control service inside the Gateway. Disabling the plugin removes the CLI, `browser.request` Gateway method, agent tool, and control service together.[^oc-plugin] Internally, the control server talks CDP to Chromium and layers Playwright over CDP for advanced actions.[^oc-internal]

OpenClaw does provide two useful non-agent integration surfaces:

1. `openclaw browser ...` CLI with machine-readable JSON.
2. An opt-in loopback HTTP API (`OPENCLAW_EAGER_BROWSER_CONTROL_SERVER=1`) with endpoints for lifecycle, profiles, tabs, snapshots, actions, cookies/storage, downloads, and debugging. It requires Gateway shared-secret auth and is deliberately loopback-only.[^oc-control-api] [^oc-security]

That makes reuse technically possible from Python (`subprocess` or HTTP), but this is not a standalone generic MCP server. The service lifecycle and security model are owned by OpenClaw Gateway.

### Headless VPS

OpenClaw directly supports the target environment. On Linux without `DISPLAY` or `WAYLAND_DISPLAY`, local managed profiles run headless automatically unless headed mode is explicitly forced. It supports global/per-profile `headless`, a one-shot `start --headless`, and reports how headless mode was selected.[^oc-headless] In Docker, Chromium binaries and system libraries must be baked into the image; OpenClaw documents its install flag and persistence requirements.[^oc-docker]

### Profiles and persistent login

OpenClaw-managed profiles use their own user-data directory and CDP port; multiple named profiles are supported. Cookies, local storage, and session storage have CLI/API operations.[^oc-profiles] [^oc-state]

For human-owned login state, OpenClaw offers:

- `user`: attach to an existing signed-in Chrome through official Chrome DevTools MCP. Initial remote-debugging attachment prompts for human consent, so a person must be at the machine.[^oc-existing]
- `chrome`: OpenClaw's extension route into signed-in Chrome, documented as working while nobody is at the desk.[^oc-profile-choice]
- remote CDP profiles, including browsers on another host or managed service.[^oc-remote]

For a VPS airline-login profile, the simplest OpenClaw mode would be a named managed persistent profile. Login/2FA would need an operator-accessible headed session at setup time (or another supported signed-in-browser/remote-CDP arrangement), after which headless reuse may work subject to airline bot controls and session expiry.

### Snapshots, refs, and sessions

OpenClaw intentionally exposes one agent browser tool with snapshot-driven actions. Snapshots return stable AI/ARIA UI trees, while `act` uses returned refs for click/type/drag/select. CSS selectors are intentionally not accepted for the normal actions; refs must be refreshed after UI/document changes.[^oc-agent-tools] [^oc-ref-contract]

Tabs receive stable `tabId` handles and optional labels over volatile CDP target IDs. Tabs opened by the browser tool are attributed to an OpenClaw session; qualifying ownership records survive Gateway restarts and are cleaned up with session lifecycle events.[^oc-tabs] [^oc-tab-ownership] This is more lifecycle machinery than a simple flight-search service likely needs, but it is robust.

### Human takeover

OpenClaw has the most integrated human-intervention UX in this comparison:

- Its Control UI Browser panel follows the session's current browser profile/host/tab; first-party release documentation says it can navigate, click, type, scroll, inspect, and annotate.[^oc-browser-panel] [^oc-browser-panel-release]
- Its browser guidance instructs the agent to surface login, 2FA, CAPTCHA, and native permission blockers as manual action rather than guessing/retrying.[^oc-manual-blockers]
- Existing signed-in Chrome modes let a person and the agent operate the same browser state, with different “person present” requirements for Chrome MCP versus extension mode.[^oc-profile-choice]

This operator experience is part of OpenClaw's Control UI and routing/session model, not a portable widget exposed by the HTTP browser API. If the flight app does not otherwise need OpenClaw, adopting the whole stack just for this feature is likely too complex.

### Reuse verdict

OpenClaw is a credible **browser sidecar platform**, but not the smallest reusable component. Choose it only if its Gateway, operator UI, remote node routing, session ownership, and signed-in Chrome integration are themselves desired product features. Otherwise Playwright MCP has a cleaner boundary.

## 3. Playwright MCP

### Standalone/tool-server fit

Playwright MCP explicitly describes itself as an MCP server that lets LLMs operate pages through structured accessibility snapshots. Requirements include Node.js 18+ and “any other MCP client”; its README includes configuration for many unrelated clients, including Gemini CLI and Codex.[^pwmcp-purpose] [^pwmcp-gemini]

It supports:

- normal stdio launch (`npx @playwright/mcp@latest`), ideal for one Python service owning one subprocess;
- standalone HTTP transport with `--port`, addressed at `http://localhost:<port>/mcp`;
- an official long-lived Docker invocation reachable by any MCP client;
- a JavaScript `createConnection()` API for embedding the server, if a Node wrapper is acceptable.[^pwmcp-standalone] [^pwmcp-docker] [^pwmcp-programmatic]

For Python + Gemini, use a standard Python MCP client and keep model orchestration separate from browser transport. Gemini only needs tool schemas/results; Playwright MCP is model-neutral.

### Headless VPS

`--headless` is a documented launch option. The official Docker image currently supports headless Chromium and its long-running example includes `--headless --browser chromium --no-sandbox --port 8931 --host 0.0.0.0`.[^pwmcp-options] [^pwmcp-docker]

This directly matches a text-only Ubuntu VPS. Prefer binding to loopback or a private interface and authenticating/isolation at the deployment layer: Playwright MCP explicitly warns that it is **not a security boundary**.[^pwmcp-security]

### Persistent profiles and sessions

Playwright MCP supports three modes:

1. **Persistent profile (default):** login data is retained on disk. Linux default is under `~/.cache/ms-playwright/...`; override with `--user-data-dir`. A profile can be used by only one browser instance at once.[^pwmcp-profiles]
2. **Isolated:** in-memory session state is discarded at browser close; bootstrap cookies/local storage with `--storage-state`.[^pwmcp-isolated]
3. **Browser extension:** connect to existing Chrome/Edge tabs and reuse logged-in browser state.[^pwmcp-extension]

The server also offers tools to save and restore storage state. For HTTP deployment, `--shared-browser-context` reuses one browser context among connected HTTP clients; otherwise design the Python application so a user/search session has a clear MCP connection/context owner.[^pwmcp-options] [^pwmcp-storage-tools]

For this project's single persistent airline-login lane, use a dedicated `--user-data-dir` and serialize use of that profile. If concurrency is later needed, allocate separate profiles or isolated contexts.

### Snapshot and element-ref interaction

The server's core is accessibility-tree interaction, not screenshots. `browser_snapshot` captures the structured snapshot. Actions such as `browser_click`, `browser_type`, and `browser_select_option` accept an exact `target` element reference from that snapshot (with unique selectors also permitted). The screenshot tool explicitly says not to use screenshots for actions.[^pwmcp-snapshot] [^pwmcp-click]

This is a good deterministic-first interface for flight search:

1. navigate;
2. snapshot;
3. locate role/name and ref;
4. call click/fill/select against that ref;
5. snapshot after page-changing actions;
6. use Gemini only to interpret ambiguous page state or choose among candidates.

The unsafe arbitrary Playwright-code tool exists but is RCE-equivalent and should be disabled/not exposed for the normal agent loop.[^pwmcp-unsafe]

### Human takeover limits

Playwright MCP has useful collaboration mechanisms, but not an all-in-one remote desktop:

- `--extension` connects the agent to an already-running, human-visible Chrome/Edge and reuses its tabs/login state.[^pwmcp-extension]
- With the opt-in DevTools capability, `browser_start_recording` records actions performed by the user as Playwright code, and `browser_annotate` opens a dashboard for human annotations.[^pwmcp-devtools]

However, on a headless VPS there is no person-visible window. The official standalone note says a headed browser on a system without a display must be run from an environment that supplies `DISPLAY`; the MCP server itself does not document a VNC/noVNC/takeover service.[^pwmcp-standalone] Therefore human login/CAPTCHA takeover requires one of:

- run the browser under Xvfb plus a separately managed VNC/noVNC desktop;
- connect Playwright MCP via CDP to an operator-accessible browser host;
- use extension mode on a person's Chrome/Edge machine.

### Reuse verdict and minimal recommendation

Adopt Playwright MCP as the browser tool server and avoid implementing click/fill/inspect initially.

Suggested deployment shape:

```text
Python flight-search service
  ├─ Gemini orchestration / deterministic workflow
  └─ MCP client (stdio or loopback HTTP)
       └─ @playwright/mcp
            └─ headless Chromium + dedicated persistent user-data-dir
```

Recommended initial choices:

- one long-lived Playwright MCP process;
- loopback stdio or HTTP only;
- `--headless`;
- explicit dedicated `--user-data-dir`;
- one serialized browser context/profile;
- core accessibility snapshot/ref tools only;
- do not expose `browser_run_code_unsafe`;
- add human takeover separately only when login/CAPTCHA evidence shows it is needed.

## Decision

1. **Use Playwright MCP for the prototype.** It provides the desired click/fill/inspect primitive set behind a standard, reusable tool-server boundary and directly supports headless VPS plus persistent authentication state.
2. **Do not reuse Codex browser/computer use.** Treat Codex as a harness that consumes browser integrations, not the source of a portable browser implementation.
3. **Keep OpenClaw as a fallback platform option**, especially if an integrated remote operator UI, browser-node routing, or signed-in Chrome extension becomes more valuable than minimal dependencies. Its browser API is capable, but adopting it only as a browser driver would add avoidable runtime coupling.
4. **Human takeover is not solved by headless Playwright MCP alone.** Plan either an operator-accessible browser/CDP/extension path or a separate VNC/noVNC display layer if airline login, 2FA, or CAPTCHA demands it.

## First-party citations

[^codex-app-server]: OpenAI Codex source, [`codex-rs/app-server/README.md`, protocol and thread/turn lifecycle](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/app-server/README.md#protocol).
[^codex-sdk]: OpenAI Codex source, [Python SDK README](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/sdk/python/README.md#openai-codex-python-sdk) and [SDK runtime explanation](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/sdk/python/docs/getting-started.md).
[^codex-features]: OpenAI Codex source, [browser/computer feature specifications](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/features/src/lib.rs#L1387-L1410).
[^codex-browser-config]: OpenAI Codex source, [`BrowserUseConfigToml`](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/config/src/browser_use.rs#L7-L22).
[^codex-computer-config]: OpenAI Codex source, [`ComputerUseConfigToml`](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/config/src/computer_use.rs#L7-L35).
[^codex-plugin-ids]: OpenAI Codex source, [discoverable plugin allowlist including Chrome and computer-use](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/core-plugins/src/discoverable.rs#L17-L48).
[^codex-policy-routing]: OpenAI Codex source, [confirmation policy injection for `node_repl` / `cua_repl`](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/core/src/mcp_tool_call.rs#L1292-L1325).
[^codex-browser-routing]: OpenAI Codex source, [browser connector and computer-use stop-hook routing test](https://github.com/openai/codex/blob/ec84e692611d50d9c79c4bdad0a5785013975a72/codex-rs/core/tests/suite/hooks_executor.rs#L365-L443).

[^oc-plugin]: OpenClaw docs, [browser plugin control and coupled surfaces](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#plugin-control).
[^oc-control-api]: OpenClaw docs, [opt-in loopback browser HTTP control API and endpoints](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser-control.md#control-api-optional).
[^oc-internal]: OpenClaw docs, [browser control internals: CDP plus Playwright](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser-control.md#how-it-works-internal).
[^oc-security]: OpenClaw docs, [browser control security/authentication](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#security).
[^oc-headless]: OpenClaw docs, [profile behavior and Linux no-display headless fallback](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#profile-behavior) and [CLI lifecycle note](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/cli/browser.md#lifecycle).
[^oc-docker]: OpenClaw docs, [Docker Playwright/Chromium installation](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser-control.md#docker-playwright-install).
[^oc-profiles]: OpenClaw docs, [profile types and dedicated user data directories](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#profiles-multi-browser).
[^oc-state]: OpenClaw CLI docs, [cookies and storage operations](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/cli/browser.md#state-and-storage).
[^oc-existing]: OpenClaw docs, [existing signed-in session via Chrome DevTools MCP](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#existing-session-via-chrome-devtools-mcp).
[^oc-profile-choice]: OpenClaw docs, [`openclaw`, `user`, and `chrome` profiles and presence requirements](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#profiles-openclaw-user-chrome).
[^oc-remote]: OpenClaw docs, [local versus remote CDP control](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#local-vs-remote-control).
[^oc-agent-tools]: OpenClaw docs, [agent browser tool and snapshot/ref mapping](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#agent-tools--how-control-works).
[^oc-ref-contract]: OpenClaw docs, [actions require snapshot refs and not CSS selectors](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser-control.md#L284-L294).
[^oc-tabs]: OpenClaw CLI docs, [stable tab IDs and labels](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/cli/browser.md#tabs).
[^oc-tab-ownership]: OpenClaw docs, [session tab cleanup ownership and persistence](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#tab-cleanup-ownership).
[^oc-browser-panel]: OpenClaw docs, [Control UI Browser panel routing and interactivity](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#browser-panel-in-the-control-ui).
[^oc-browser-panel-release]: OpenClaw first-party release docs, [Browser panel navigation, input, inspection, and annotation](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/releases/2026.8.1.md#L1545-L1561).
[^oc-manual-blockers]: OpenClaw docs, [manual handling of login, 2FA, CAPTCHA, and permission blockers](https://github.com/openclaw/openclaw/blob/84bbb1db5d2aa9bea8f37695a690f16e655b3a45/docs/tools/browser.md#L91-L103).

[^pwmcp-purpose]: Microsoft Playwright MCP, [purpose, key features, and requirements](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#playwright-mcp).
[^pwmcp-codex]: Microsoft Playwright MCP, [official Codex MCP configuration](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#codex).
[^pwmcp-gemini]: Microsoft Playwright MCP, [official Gemini CLI configuration reference](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#gemini-cli).
[^pwmcp-options]: Microsoft Playwright MCP, [configuration options](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#configuration).
[^pwmcp-profiles]: Microsoft Playwright MCP, [persistent profile behavior and concurrency constraint](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#persistent-profile).
[^pwmcp-isolated]: Microsoft Playwright MCP, [isolated sessions and storage state](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#isolated).
[^pwmcp-extension]: Microsoft Playwright MCP, [browser extension and existing logged-in tabs](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#browser-extension).
[^pwmcp-standalone]: Microsoft Playwright MCP, [standalone MCP HTTP server](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#standalone-mcp-server).
[^pwmcp-security]: Microsoft Playwright MCP, [security warning](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#security).
[^pwmcp-docker]: Microsoft Playwright MCP, [official headless-Chromium Docker deployment](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#docker).
[^pwmcp-programmatic]: Microsoft Playwright MCP, [programmatic `createConnection` usage](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#programmatic-usage).
[^pwmcp-snapshot]: Microsoft Playwright MCP, [`browser_snapshot` and screenshot guidance](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#L1062-L1085).
[^pwmcp-click]: Microsoft Playwright MCP, [`browser_click` exact snapshot target-ref input](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#L869-L878).
[^pwmcp-storage-tools]: Microsoft Playwright MCP, [save/restore storage-state tools](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#L1335-L1352).
[^pwmcp-unsafe]: Microsoft Playwright MCP, [arbitrary-code tool RCE warning](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#L1043-L1049).
[^pwmcp-devtools]: Microsoft Playwright MCP, [human annotation and user-action recording tools](https://github.com/microsoft/playwright-mcp/blob/4c1fb03bad3bae379b0ae0e3d81d2660de56bd91/README.md#devtools-opt-in-via---capsdevtools).
