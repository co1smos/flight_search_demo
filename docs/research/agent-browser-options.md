# Agent-oriented browser automation options for Gemini on a headless VPS

**Research date:** 2026-09-03
**Scope:** Python application on a headless Ubuntu VPS. Google Gemini receives a goal and operates a browser; the application should not implement a low-level browser-tool abstraction. Primary use is recurring, read-only award-search automation on Air Canada Aeroplan and ANA Mileage Club, with persistent authentication.

## Executive recommendation

1. **Start with `browser-use` for the ANA/fallback agent.** It is the simplest direct fit: a Python agent accepts a task, has a native `ChatGoogle` Gemini adapter, launches Chromium locally in headless mode, persists either a Chrome user-data directory or Playwright-style storage state, validates final output with Pydantic, and exposes explicit step/action/failure/time limits. It also has a particularly useful local `allowed_domains` control for bounding an agent to ANA/Aeroplan domains. These are open-source library features and do not require Browser Use Cloud.[^bu-quickstart][^bu-models][^bu-browser][^bu-agent]
2. **Keep deterministic Playwright/site-specific code for Air Canada.** Stagehand is strongest when mixing known deterministic actions with AI `act`/`extract`, but its autonomous `execute` mode is not a clearer or simpler Gemini agent than browser-use for this project. Stagehand becomes more attractive if the product wants AI-assisted selectors and caching/self-healing inside a mostly deterministic workflow.[^stagehand-readme]
3. **Evaluate Skyvern if operational features outweigh simplicity.** Skyvern is the strongest current alternative found: it supplies a higher-level task/workflow service, Gemini configuration, structured extraction, persistent browser profiles, credentials/TOTP integrations, browser livestreaming, and self-hosted deployment. The trade-off is substantially more infrastructure and framework surface than browser-use.[^skyvern-readme][^skyvern-compose][^skyvern-profiles]
4. **Do not confuse library and hosted-browser capabilities.** Browserbase Contexts and interactive Live View are hosted Browserbase features used with Stagehand; Browser Use cloud profiles/live URLs are Browser Use Cloud features. The corresponding open-source libraries can run a browser locally, but they do not magically provide a secure remote human-takeover UI on a text-only VPS.[^bb-contexts][^bb-live-view][^bu-quickstart]

## Capability matrix

Legend: **Yes** = directly supported and verified; **Partial** = possible but with an important limitation or external component; **Cloud** = supplied by the vendor's hosted service rather than the local library.

| Capability | browser-use (open-source Python) | Stagehand Python v3 | Skyvern |
|---|---|---|---|
| Goal-driven autonomous browser | **Yes.** `Agent(task=..., llm=...)` owns the browser action loop. | **Yes.** `session.execute()` runs an autonomous agent; `act`/`observe` are narrower primitives. | **Yes.** `run_task()` / `page.agent.run_task()` and workflows. |
| Native Gemini | **Yes.** `ChatGoogle`, Google AI Studio API key, or Vertex AI. | **Yes.** Generic Google provider and an official Vertex/Gemini example; model configuration is passed to Stagehand operations. | **Yes.** Self-host config explicitly supports `ENABLE_GEMINI`, a Gemini model key, and `GEMINI_API_KEY`. |
| Local headless Ubuntu | **Yes.** Chromium runs locally; `headless=True`, with automatic display detection by default. Docker files are supplied. | **Yes.** `server="local"`, browser type `local`, and `launchOptions.headless=True`; local mode starts the bundled SEA server/binary. | **Yes.** `chromium-headless` or `launch_local_browser(headless=True)`; pip and Docker Compose self-host paths exist. |
| Headed mode | **Yes.** `headless=False`. | **Yes.** local browser launch option `headless=False`. | **Yes.** `chromium-headful` / `headless=False`. |
| Persistent local profile | **Yes.** `user_data_dir`, named Chrome `profile_directory`, or exported/loaded `storage_state`. | **Yes.** local launch options include `userDataDir` and `preserveUserDataDir`. This is lower-level configuration, not a Stagehand account/profile manager. | **Yes.** local browser accepts `user_data_dir` and launches a persistent Playwright context. Workflow/profile APIs add managed persistence. |
| Persistent hosted profile | **Cloud.** `cloud_profile_id` and profile sync are Browser Use Cloud. | **Cloud.** Browserbase Contexts persist cookies and application data across sessions; Stagehand's start schema exposes Browserbase context ID + `persist`. | **Yes in Skyvern service; cloud or self-host depends deployment/storage configuration.** `persist_browser_session` and reusable browser profiles are part of Skyvern's task/workflow API. |
| Structured extraction/final result | **Yes.** Pydantic `output_model_schema`; parsed value at `history.structured_output`. | **Yes.** `extract(instruction, schema=JSON Schema)`; the Python SDK also converts responses to Pydantic models. | **Yes.** `page.extract(prompt, schema)` and task `data_extraction_schema`. |
| Human login/takeover | **Partial locally.** Headed browser plus `agent.pause()`/`resume()` can support manual intervention if a display/VNC is arranged. **Cloud:** sandbox creation returns a `live_url`, and cloud profile sync supports authenticated state. | **Partial locally.** A local headed browser can be exposed through the operator's own desktop/VNC setup, but Stagehand Python does not expose a first-class local takeover URL. **Cloud:** Browserbase Session Live View is an interactive window that can display or control the browser, and official Context docs describe logging in manually through Live View. | **Strongest integrated story.** The project documents livestreaming “and intervening when necessary,” saved browser profiles, credential-backed login, and TOTP methods. Local deployment still needs the UI/browser-streaming path to be securely exposed to the operator. |
| Agent action/step limits | **Yes.** `run(max_steps=...)`, `max_actions_per_step`, and `max_failures`. | **Yes.** `execute_options.max_steps`; `tool_timeout` per agent tool call. | **Yes.** `MAX_STEPS_PER_RUN`; current CLI also exposes `--max-steps`. |
| Timeouts | **Yes.** model-specific `llm_timeout`, per-step `step_timeout`, plus individual browser-event timeouts. | **Yes.** request timeout (60 seconds by default), per-request override, agent `tool_timeout`, DOM settle/connect/session timeouts. | **Yes.** browser action timeout, task timeout/current CLI `--task-timeout`, workflow wait timeout, and browser-session timeout controls. |
| Domain/search-only guardrail | **Best local option.** Browser `allowed_domains` / `prohibited_domains` enforce navigation restrictions. Bound steps and remove/avoid mutation-oriented custom tools. | **Partial.** Bound `max_steps`; `use_search` only controls Browserbase Search API availability. Browserbase separately offers hosted allowed-domain restrictions, but that is not evidenced as a local Stagehand agent policy in the Python SDK. | **Partial.** Bound steps/time and use task/workflow prompts; deterministic workflow blocks can narrow behavior. No equally simple local domain allowlist was verified in the reviewed public high-level API. |
| Self-hosting | **Yes.** MIT Python library and local Chromium; Docker build supplied. Cloud stealth/proxy/CAPTCHA/profile-sync features remain hosted extras. | **Yes, with nuance.** Python v3 local mode starts an embedded Stagehand SEA server and local browser; remote mode calls the hosted Stagehand API and normally Browserbase. Browserbase Contexts/Live View/stealth are hosted features. | **Yes.** Full local server/UI via pip or Docker Compose, with SQLite or Postgres. Skyvern Cloud adds managed anti-bot, proxies, CAPTCHA solving, and parallel infrastructure. |
| Implementation weight | **Lowest** for “Gemini, do this browser task.” | **Low-to-medium** for AI-assisted deterministic automation; somewhat more explicit session/operation plumbing for autonomous runs. | **Highest** operational footprint, but richest built-in workflow/auth/observability layer. |

## Detailed findings

### 1. browser-use

#### Why it fits

The open-source package provides the complete agent/browser loop: application code supplies a natural-language `task`, an LLM object, and optional browser/agent policies. The official quickstart shows Gemini as `ChatGoogle(model=...)` passed directly to `Agent`; the model guide supports both Google API keys and Vertex AI.[^bu-quickstart][^bu-models]

For a recurring authenticated search, local persistence has two useful levels:

- `Browser(user_data_dir=..., profile_directory=...)` uses a persistent Chrome profile directory.
- `export_storage_state()` and `Browser(storage_state=...)` save and restore cookies/local storage; official guidance calls this the production/CI/headless strategy and says state is auto-saved periodically and at shutdown.[^bu-browser]

On a text-only VPS, explicitly set `headless=True`. The library supports both headless and headed operation and otherwise auto-detects whether a display exists. It can also attach to any existing CDP browser.[^bu-browser][^bu-profile-source]

For machine-consumed flight results, define a Pydantic `output_model_schema`; the completed history exposes a parsed `structured_output`. Bounded operation is straightforward: `max_steps`, `max_actions_per_step`, `max_failures`, `llm_timeout`, and `step_timeout` are all public controls. Individual navigation, click, typing, scrolling, and storage events have separate timeout settings.[^bu-agent]

Most importantly for “search-only,” the local browser has an actual navigation policy: `allowed_domains` and `prohibited_domains`. This is stronger than relying only on the task prompt. A production ANA configuration should allow only the exact ANA login/award-search domains and any known identity-provider domains required by login, keep a conservative `max_steps`, and validate the final URL/result schema.[^bu-browser]

#### Human login and cloud distinction

The local API exposes `agent.pause()` and `agent.resume()`, and headed mode can be used for intervention. On a headless VPS, however, the project must still supply the human-access channel (for example, a tightly secured VNC/noVNC display or an externally managed CDP browser). That channel is not a built-in local browser-use takeover service.[^bu-agent][^bu-browser]

Browser Use Cloud is separate. The `@sandbox`/cloud browser configuration supports a cloud profile ID, callback data containing a `live_url`, cloud timeouts, proxies, and profile sync. The repository explicitly distinguishes the free local agent from the fully hosted cloud agent and attributes scalable infrastructure, stealth, proxy rotation, CAPTCHA handling, and persistent cloud facilities to the hosted product.[^bu-quickstart][^bu-readme]

#### Caveats

- Persistence does not guarantee a site will keep accepting the login; ANA may expire or revoke cookies and may challenge a VPS/browser fingerprint.
- The open-source library provides the agent and browser controls, but production stealth/proxy/CAPTCHA claims in the docs are cloud recommendations, not local guarantees.[^bu-readme]
- `max_steps` is the run's hard loop bound, but a step may contain multiple actions; set `max_actions_per_step` as well.[^bu-agent]

### 2. Stagehand Python v3

#### What Stagehand is best at

Stagehand deliberately combines deterministic browser code with natural-language operations. Its README positions `act`, `observe`, caching, and self-healing as a bridge between Playwright-style precision and agentic flexibility. That is compelling for Air Canada if known paths remain deterministic while AI handles selector drift.[^stagehand-readme]

The Python v3 API also has a full autonomous operation: `session.execute()` accepts a natural-language instruction and `max_steps`, while `agent_config` selects DOM, hybrid, or computer-use mode and the model. Structured extraction is first-class through `session.extract(..., schema=<JSON Schema>)`.[^stagehand-readme][^stagehand-execute][^stagehand-extract]

Gemini support is verified in the generated SDK types (`google` is a model provider) and by the official Vertex example, which runs `observe`, schema extraction, and autonomous `execute` with `vertex/gemini-2.5-flash` in both remote and local modes.[^stagehand-execute][^stagehand-vertex]

#### Local browser and persistence

The Python SDK's local example runs `Stagehand(server="local")`, starts an embedded SEA server, and launches a local headless browser. The session start schema exposes `headless`, `userDataDir`, and `preserveUserDataDir`, so a stable local profile directory can retain authenticated state across launches.[^stagehand-local][^stagehand-start]

This is genuine local execution, but it is architecturally different from browser-use: the Python package is an API client around a Stagehand server, and local mode boots a bundled SEA executable. Remote mode calls `https://api.stagehand.browserbase.com` by default.[^stagehand-readme][^stagehand-local]

#### Hosted Browserbase features are not local Stagehand features

When Stagehand uses `browser={"type": "browserbase"}`, Browserbase supplies the browser. Browserbase Contexts persist the Chromium user-data directory (cookies and application data) across sessions. The official Context workflow explicitly says a user may log in manually through Session Live View, end the session with persistence enabled, and reuse the same Context later; Contexts live until deletion, though website sessions can still expire.[^stagehand-start][^bb-contexts]

Browserbase Session Live View is documented as an interactive window that can display **or control** a browser session. This is the clean hosted human-takeover/login path. It should not be represented as functionality of Stagehand's self-hosted local browser.[^bb-live-view]

#### Limits and caveats

- Autonomous runs have `max_steps` and per-tool `tool_timeout`. HTTP requests default to a 60-second timeout and support a per-request override; the official example uses a five-minute request timeout for `execute`.[^stagehand-execute][^stagehand-readme][^stagehand-full-example]
- Stagehand's `use_search` option enables Browserbase Search API; it is not a “read-only browser” switch. A search-only safety policy still needs prompt/system-policy design and, locally, external URL/action enforcement if required.[^stagehand-execute]
- For this product's simplest goal-driven ANA fallback, Stagehand requires more explicit session/start/navigate/execute/extract lifecycle code than browser-use, without a verified local guardrail as convenient as `allowed_domains`.

### 3. Skyvern — strongest broader alternative

Skyvern is more than an embedded library: it is an open-source browser-automation service and workflow system with a Python SDK. It supports natural-language `page.act`, schema-based `page.extract`, validation, arbitrary prompts, and higher-level `page.agent.run_task`; it can also mix normal Playwright actions, AI-located actions, and selector-first/AI-fallback actions.[^skyvern-readme]

For Gemini self-hosting, the official Docker configuration explicitly lists `ENABLE_GEMINI=true`, a Gemini `LLM_KEY`, and `GEMINI_API_KEY`. The project supports local installation/UI/server through pip and a full Docker Compose deployment.[^skyvern-compose][^skyvern-readme]

Authentication and state are unusually comprehensive:

- Local `launch_local_browser()` accepts `headless` and `user_data_dir` and uses Playwright's persistent context.[^skyvern-local-browser]
- Workflow `persist_browser_session` reuses and updates the same user-data directory between workflow runs.[^skyvern-workflow-persistence]
- Browser Profiles archive full state (cookies, storage, session files), can be created from a workflow or browser session, and can seed later runs.[^skyvern-profiles]
- The high-level agent exposes credential login; the project documents Bitwarden/custom credential service and TOTP methods.[^skyvern-readme]
- The project documents browser viewport livestreaming for debugging, understanding actions, and intervention.[^skyvern-readme]

Skyvern also exposes useful bounds: `BROWSER_ACTION_TIMEOUT_MS`, `MAX_STEPS_PER_RUN`, current CLI `--max-steps`, and `--task-timeout` (10–1800 seconds). Headless/headful browser types are explicitly configured.[^skyvern-env][^skyvern-cli]

The cost is complexity. A self-hosted Skyvern deployment includes a service, persistence, and optionally a UI/database rather than one lightweight in-process agent. For a product that only needs one recurring ANA search flow, start with browser-use. Move to Skyvern if browser operations, credentials/2FA, intervention, workflow auditability, or multi-run profile management become the dominant engineering problem.

## Alternatives considered but not promoted

- **Direct Gemini computer-use + Playwright/CDP:** would force this project to implement and maintain the very browser tool/action loop it wants to avoid.
- **Playwright/Selenium alone:** excellent deterministic browser drivers, but not goal-oriented agents.
- **MCP browser servers:** useful when an already-running external agent is the orchestrator, but they move the low-level browser tool surface into another process rather than providing the clean embedded Python `goal -> bounded result` abstraction requested here.
- **Magnitude:** the current official repository is centered on a broader coding/desktop agent application rather than a focused Python browser-agent library for embedding in this service; it was not a stronger match than the three options above.

## Suggested implementation shape

### Initial choice: browser-use locally

- Run Chromium headless on the VPS with a dedicated, nonshared `user_data_dir` per loyalty program/account.
- Bootstrap login once through a temporary secured headed/VNC session or on another trusted machine, then copy only the dedicated automation profile/storage state to the VPS. Never point automation at a personal everyday Chrome profile.
- Configure Gemini through `ChatGoogle` and use a Pydantic result model for itinerary, cabin, points, taxes, dates, and source URL.
- Enforce `allowed_domains`, a low `max_steps`, a low `max_actions_per_step`, `max_failures`, `llm_timeout`, and `step_timeout`.
- Phrase the task as explicitly read-only: search and extract only; never purchase, transfer, change profile/account data, or submit a booking. Treat prompt restrictions as secondary to URL/action controls.
- Serialize runs per account/profile. Reusing one authenticated profile concurrently risks profile locking and site-side session invalidation.
- Detect logged-out/challenge states explicitly and route them to a human re-authentication workflow rather than allowing an unbounded agent retry loop.

### Escalation paths

- Use **Stagehand** for deterministic Air Canada flows if its AI-assisted selectors/caching reduce selector maintenance.
- Use **Browserbase with Stagehand** if managed Contexts, interactive Live View, proxies, or browser infrastructure are worth a hosted dependency.
- Use **Skyvern** if the product needs an integrated self-hosted task/workflow UI, credential/TOTP handling, livestream intervention, and managed reusable browser profiles.

## Sources

All sources are first-party documentation or first-party repository source. GitHub source citations are pinned to the reviewed commits.

[^bu-quickstart]: Browser Use, “Quickstart & Production Deployment,” including Gemini, local agent, cloud profile sync, live URL callback, and cloud timeout parameters: https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/skills/open-source/references/quickstart.md
[^bu-models]: Browser Use, “Supported LLM Models,” Google Gemini / Vertex AI: https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/skills/open-source/references/models.md
[^bu-browser]: Browser Use, “Browser Configuration,” headless mode, user-data/profile/storage state, domain restrictions, remote/cloud distinction, and authentication strategies: https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/skills/open-source/references/browser.md
[^bu-agent]: Browser Use, “Agent Configuration & Behavior,” structured output, pause/resume, step/action/failure limits, and timeouts: https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/skills/open-source/references/agent.md
[^bu-profile-source]: Browser Use source, browser launch/profile model (`headless`, persistent-context parameters, Docker args): https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/browser_use/browser/profile.py
[^bu-readme]: Browser Use README, Python library, open-source vs cloud, authentication, CAPTCHA, and production distinctions: https://github.com/browser-use/browser-use/blob/5b50d1f511189c9df93e7f0bcb9da943b2d5780b/README.md
[^stagehand-readme]: Stagehand Python README, framework purpose, local/remote modes, structured extraction, autonomous execute, client defaults, and request timeouts: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/README.md
[^stagehand-local]: Stagehand Python official local-mode example, embedded SEA server and headless local browser: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/examples/local_example.py
[^stagehand-start]: Stagehand Python generated session-start schema, local/browserbase types, `headless`, `userDataDir`, `preserveUserDataDir`, Browserbase Context persistence, keep-alive, and session timeout: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/src/stagehand/types/session_start_params.py
[^stagehand-execute]: Stagehand Python generated execute schema, Google provider, agent modes, `maxSteps`, `toolTimeout`, and `useSearch`: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/src/stagehand/types/session_execute_params.py
[^stagehand-extract]: Stagehand Python generated extraction schema (`instruction`, JSON Schema, model options): https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/src/stagehand/types/session_extract_params.py
[^stagehand-vertex]: Stagehand Python official Vertex/Gemini example for remote and local operation: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/examples/vertex_auth_example.py
[^stagehand-full-example]: Stagehand Python full example, schema extraction and autonomous execution with explicit `max_steps` and request timeout: https://github.com/browserbase/stagehand-python/blob/a2af713d1f5477f13ac47530a964b45fa40be5fa/examples/full_example.py
[^bb-contexts]: Browserbase official Contexts documentation, persistent user data, manual login through Live View, reuse, and expiry behavior: https://docs.browserbase.com/platform/browser/core-features/contexts
[^bb-live-view]: Browserbase official Session Live View documentation, interactive display/control of a session: https://docs.browserbase.com/platform/browser/observability/session-live-view
[^skyvern-readme]: Skyvern README, self-hosting, SDK commands, schema extraction, agent tasks/login, cloud distinction, authentication/TOTP, and livestreaming: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/README.md
[^skyvern-compose]: Skyvern Docker Compose configuration, first-party Gemini settings: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/docker-compose.yml
[^skyvern-local-browser]: Skyvern source, `launch_local_browser`, headless/headed operation, user-data directory, and persistent Playwright context: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/skyvern/library/skyvern.py#L484-L559
[^skyvern-workflow-persistence]: Skyvern official workflow documentation, `persist_browser_session`: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/fern/workflows/creating-workflows.mdx#L78-L82
[^skyvern-profiles]: Skyvern official Browser Profiles documentation, archived full browser state and reuse: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/fern/browser-sessions/browser-profiles.mdx
[^skyvern-env]: Skyvern `.env.example`, headless/headful modes, action timeout, and maximum steps: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/.env.example#L117-L134
[^skyvern-cli]: Skyvern CLI source, browser task `--max-steps` and `--task-timeout`: https://github.com/Skyvern-AI/skyvern/blob/cc1509eae3a3c8f0ec7e5aee6c85c5191d99ba2d/skyvern/cli/commands/browser.py#L2839-L2875
