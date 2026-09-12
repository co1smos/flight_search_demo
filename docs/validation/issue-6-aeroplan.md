# Controlled Aeroplan validation: UNVERIFIED

One controlled live attempt was performed for issue #6. Continuous live execution
remains disabled. The operator explicitly acknowledged account/terms risk and
authorized one search: Aeroplan, SFO to TPE, 2026-09-14, Business, 1 adult,
one-way, maximum 105000 points. No credentials were accessed. No remote
acceptance evidence is claimed.

The authorized validation entry point was executed on 2026-09-12 against the
configured loopback Steel service and its persistent Chromium profile. It reached
the official Air Canada entry page and exited 1 as designed, reporting
`CHALLENGE_BLOCKED`. Both operator gates passed. No form was filled and no search
was submitted. The recorded allowance remains 10. The browser session was closed
without terminating the persistent profile, but no result was extracted or
validated. This is exact local evidence of the controlled attempt, not evidence
of availability.

Committed evidence:

- [Original structured request](issue-6-request.json)
- [Confirmation bound to the request hash](issue-6-confirmation.json)
- [Actual correlated blocked report](issue-6-report.jsonl)

The confirmation records the authorization supplied in this issue's worker
prompt. It does not authorize alternate criteria, retries, or continuous runs.

Repository history and every related `sandcastle/issue-5-*` branch were audited,
including `b4f0bdf`, `5690b7f`, `d83f087`, and the latest `2731bc0` history.
That implementation contains useful search-only policy, visible-result
validation, Playwright lifecycle handling, and browser-use evidence collection.
The controlled driver now reuses the repository's browser-session acquisition,
search-only policy, and result validator against the persistent Steel browser.
It fails closed before submission when the live page is blocked or its form is
not deterministically recognized.

## Reviewable blocked report

Use an actual structured Aeroplan request and its existing request-hash
confirmation format. Acknowledgement is a separate explicit operator action:

```sh
uv run python -m flight_search_demo.aeroplan_validation \
  --request docs/validation/issue-6-request.json --confirmation docs/validation/issue-6-confirmation.json \
  --event-log .artifacts/aeroplan-validation.jsonl \
  --steel-base-url http://127.0.0.1:3000 \
  --acknowledge-account-and-terms-risk
```

This exits 1 while continuous live execution remains disabled, appends a
correlated report, and prints the controlled outcome.
Omitting acknowledgement reports `RISK_ACKNOWLEDGEMENT_REQUIRED`; stale or
missing confirmation reports `CONFIRMATION_REQUIRED`. The report records that
no submission occurred. It includes the original request, normalized criteria,
request hash, task ID, official entry URL, and persistent remaining allowance.

The dedicated `.artifacts/aeroplan-usage.sqlite3` ledger starts at 10, has no
daily reset, and is independent of Gemini usage. Atomic submission reservations
cannot go negative and are never refunded on timeout. This blocked entry point
only reads the allowance; login and recovery do not consume it. Keep the same
ledger path across future runs; deleting or replacing it loses accounting.

## Controlled validation result

The operator gates are satisfied for the recorded request. The controlled browser
reached an Air Canada challenge/blocking page, so the adapter remains
`UNVERIFIED`. It did not attempt stealth, CAPTCHA solving, proxy changes, alternate
dates, or any other circumvention. A future operator-authorized attempt may proceed
only after the external challenge condition is resolved normally.

The implementation now:

- Covers setup, navigation, any model call, submission, visible extraction,
  cleanup, and cancellation with one controller-enforced 60-second deadline.
- Enforces approved navigation/actions before execution, binds submitted values
  to confirmed criteria, and reserves one allowance immediately before dispatch.
  Never retries an ambiguous submission; timeout still consumes its reservation.
- Validates visible results against the submitted route/date/cabin/passengers,
  classifies login/challenge/blocking/site errors and incomplete extraction
  separately, and preserves the profile without exposing credentials.
- Emits correlated live evidence and requires submission plus visible validation
  before any availability status can mark the adapter verified.

No stealth, CAPTCHA bypass, proxy rotation, private endpoint replay, or other
circumvention is implemented or authorized by this entry point. An operator
acknowledgement cannot enable continuous execution or turn fixture evidence into
live verification.
