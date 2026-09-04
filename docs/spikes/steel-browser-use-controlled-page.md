# Steel + browser-use controlled-page spike

This spike proves the real `Gemini -> browser-use -> self-hosted Steel` path on a harmless local page.

## Scope

- Use self-hosted Steel bound to loopback on port `3000`.
- Drive the session through `browser-use` over CDP.
- Use Gemini through `ChatGoogle`.
- Constrain the browser to the controlled page host via `allowed_domains`.
- Return schema-validated output with `ControlledPageResult`.
- Persist page state across Steel sessions via `browser-use` `storage_state`.
- Pause for human takeover and require a separate explicit resume operation before automation can continue.

## Files

- `src/flight_search_demo/live_run.py`
- `src/flight_search_demo/spike.py`
- `src/flight_search_demo/controlled_page.py`
- `tests/test_spike_contract.py`
- `tests/test_controlled_page.py`

## Automated verification

Run the deterministic tests in a Python `3.12` container:

```bash
sudo docker run --rm \
  --network host \
  -v "$PWD":/workspace \
  -w /workspace \
  python:3.12-slim \
  bash -lc "python -m pip install -e . && python -m unittest discover -s tests -v"
```

## Live end-to-end run

Start Steel privately first:

```bash
sudo docker run -d --name steel-browser-local --restart unless-stopped --shm-size=2g \
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:9223:9223 \
  ghcr.io/steel-dev/steel-browser:latest

curl --fail --silent http://127.0.0.1:3000/v1/health
```

Verify both published ports are bound to `127.0.0.1`, never a public interface.

Start the controlled page as a sidecar container on Docker's `bridge` network so the Steel browser can reach it directly.

```bash
CONTROLLED_PAGE_CID=$(sudo docker run -d --rm \
  --network bridge \
  -v "$PWD":/workspace \
  -w /workspace \
  python:3.12-slim \
  bash -lc "python -m pip install -e . && python -m flight_search_demo.controlled_page_server --host 0.0.0.0 --port 8765")

CONTROLLED_PAGE_IP=$(sudo docker inspect "$CONTROLLED_PAGE_CID" --format '{{.NetworkSettings.Networks.bridge.IPAddress}}')
STEEL_IP=$(sudo docker inspect "$(sudo docker ps -q --filter ancestor=ghcr.io/steel-dev/steel-browser:latest)" --format '{{.NetworkSettings.Networks.bridge.IPAddress}}')

sudo docker run --rm \
  --network bridge \
  -e GOOGLE_API_KEY \
  -e GEMINI_API_KEY \
  -e STEEL_API_KEY \
  -v "$PWD":/workspace \
  -w /workspace \
  python:3.12-slim \
  bash -lc "python -m pip install -e . && python -m flight_search_demo.live_run --steel-base-url http://$STEEL_IP:3000 --controlled-page-public-origin http://$CONTROLLED_PAGE_IP:8765 --marker marker-2026-09-03"

sudo docker stop "$CONTROLLED_PAGE_CID"
```

Expected outcomes:

- `initial_result.marker_value` matches the requested marker.
- `persisted_result.marker_value` matches the same marker in a later Steel session.
- `offsite_rejection` contains the browser-use allowlist failure from attempting `https://example.com/`.
- `.artifacts/controlled-page/handoff.json` is created with mode `600`, and `handoff.status` remains `waiting_for_human` until the explicit resume operation supplies the token.

## Human takeover procedure

1. Start a run and read `.artifacts/controlled-page/handoff.json`.
2. Expose the loopback-only Steel viewer through SSH port forwarding or an authenticated application route.
   From the operator computer: `ssh -L 3000:127.0.0.1:3000 <vps>`.
3. Pause automation while the gate is in `waiting_for_human`.
4. In a separate terminal, run `python -m flight_search_demo.resume_handoff` and enter the exact `resume_token` when prompted.

The live runner does not print the viewer URL or token to stdout. It writes those details only to the private handoff file, pauses, and resumes only after the separate command records explicit completion.
