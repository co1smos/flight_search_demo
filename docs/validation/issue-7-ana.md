# Controlled ANA validation: MANUAL_SEARCH_ONLY

One controlled live attempt was performed for issue #7 after all fixture and
repository tests passed. The operator explicitly acknowledged ANA Mileage Club
account and terms risk and authorized only this search: SFO to NRT, 2026-09-17,
Business, 1 adult, one-way, maximum 80000 miles. No credentials were accessed,
and no remote acceptance evidence is claimed.

The authorized validation entry point was executed on 2026-09-12 against the
dedicated loopback ANA Steel endpoint and persistent ANA storage-state path. It
exited 1 as designed with `MANUAL_SEARCH_ONLY` because the controlled browser
endpoint was unavailable (`URLError`). No page navigation, form interaction, or
search submission occurred. The independent ANA allowance therefore remains 10.
Continuous live execution remains disabled, and fixture behavior remains usable.

Committed evidence:

- [Original structured request](issue-7-request.json)
- [Confirmation bound to the request hash](issue-7-confirmation.json)
- [Actual correlated blocked report](issue-7-report.jsonl)

The attempt used the official ANA award entry URL and the dedicated ANA browser
configuration (`127.0.0.1:3001`, debugger port `9224`). It did not try another
date, retry the live search, use an Aeroplan profile or endpoint, or attempt any
circumvention. The blocked result keeps the adapter `UNVERIFIED` and records
`CONTROLLED_BROWSER_UNAVAILABLE` as the reason.

The implementation also fails closed if a supplied live driver raises an
unexpected runtime exception: it emits a correlated `MANUAL_SEARCH_ONLY` report,
redacts exception details to their type, preserves the allowance unless a submit
was already reserved, and leaves continuous execution disabled.
