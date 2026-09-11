# Controlled Aeroplan validation: UNVERIFIED

No live search was performed for issue #6. Continuous live execution remains
disabled. The issue prompt supplied neither an explicit account/terms risk
acknowledgement nor a confirmed search request. No credentials or persistent
Aeroplan profile were accessed. No remote acceptance evidence is claimed.

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
  --request request.json --confirmation confirmation.json \
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

A future implementation needs an operator-confirmed request and risk acceptance,
a reusable persistent Aeroplan profile, and a verified browser driver that:

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
