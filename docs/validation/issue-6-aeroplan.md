# Controlled Aeroplan validation: UNVERIFIED

No live search was performed for issue #6. Continuous live execution remains
disabled. The operator explicitly acknowledged account/terms risk and authorized
one search: Aeroplan, SFO to TPE, 2026-09-14, Business, 1 adult, one-way,
maximum 105000 points. No credentials or persistent Aeroplan profile were
accessed. No remote acceptance evidence is claimed.

The authorized validation entry point was executed on 2026-09-11. It exited 1
as designed, reporting `MANUAL_SEARCH_ONLY` with `LIVE_DRIVER_UNAVAILABLE`.
Both operator gates passed; the missing live driver is the blocker. The recorded
allowance is 10 because no search was submitted. This is local blocked evidence,
not evidence of navigation, authentication, availability, or profile reuse.

Committed evidence:

- [Original structured request](issue-6-request.json)
- [Confirmation bound to the request hash](issue-6-confirmation.json)
- [Actual correlated blocked report](issue-6-report.jsonl)

The confirmation records the authorization supplied in this issue's worker
prompt. It does not authorize alternate criteria, retries, or continuous runs.

The existing Aeroplan implementation is fixture-backed. The Steel/browser-use
integration validates a controlled test page, not the official award flow.
The fixture extraction payload and page-kind markers are not evidence that the
current Air Canada DOM can be searched or parsed. Therefore the controlled
validation entry point always stops with `MANUAL_SEARCH_ONLY` / `UNVERIFIED`
after checking confirmation and risk acknowledgement. It never falls back to
fixture success or calls a browser/model. The normal demo remains offline.

## Reviewable blocked report

Use an actual structured Aeroplan request and its existing request-hash
confirmation format. Acknowledgement is a separate explicit operator action:

```sh
uv run python -m flight_search_demo.aeroplan_validation \
  --request docs/validation/issue-6-request.json --confirmation docs/validation/issue-6-confirmation.json \
  --event-log .artifacts/aeroplan-validation.jsonl \
  --acknowledge-account-and-terms-risk
```

This exits 1, appends a correlated report, and prints the blocked outcome.
Omitting acknowledgement reports `RISK_ACKNOWLEDGEMENT_REQUIRED`; stale or
missing confirmation reports `CONFIRMATION_REQUIRED`. The report records that
no submission occurred. It includes the original request, normalized criteria,
request hash, task ID, official entry URL, and persistent remaining allowance.

The dedicated `.artifacts/aeroplan-usage.sqlite3` ledger starts at 10, has no
daily reset, and is independent of Gemini usage. Atomic submission reservations
cannot go negative and are never refunded on timeout. This blocked entry point
only reads the allowance; login and recovery do not consume it. Keep the same
ledger path across future runs; deleting or replacing it loses accounting.

## Remaining live validation gates

The operator gates are satisfied for the recorded request. Live validation still
requires a reusable persistent Aeroplan profile and a verified browser driver that:

- Covers setup, navigation, model calls, submission, visible extraction and
  cancellation with one 60-second deadline. The current report's 60-second field
  is the required limit, not evidence of a tested live timeout implementation.
- Enforces approved navigation/actions before execution, binds submitted values
  to confirmed criteria, and reserves one allowance immediately before dispatch.
  Never retries an ambiguous submission; timeout still consumes its reservation.
- Validates visible results against the submitted route/date/cabin/passengers,
  classifies login/challenge/blocking/site errors and incomplete extraction
  separately, and preserves the profile without exposing credentials.
- Emits actual correlated live evidence before any verified status can be used.

No stealth, CAPTCHA bypass, proxy rotation, private endpoint replay, or other
circumvention is implemented or authorized by this entry point. An operator
acknowledgement cannot enable continuous execution or turn fixture evidence into
live verification.
