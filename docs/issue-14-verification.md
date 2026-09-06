# Issue #14 acceptance mapping — v9 correction

Base: `4647ad0` (v8). This iteration changes only deferred policy initialization
failure handling, propagation of policy diagnostics into request outcomes, and
validation of the public parser constructor's fallback configuration.

## Vertical RED/GREEN evidence

Tests in `tests/test_issue_14_v9_corrections.py` use temporary databases, real
exclusive SQLite locks, and a recording fake client. No live Gemini calls are used.

1. RED: `test_deferred_initialization_lock_returns_classified_timeout_without_usage`
   leaked `sqlite3.OperationalError: database is locked`;
   `test_locked_initialization_emits_gemini_timeout_through_request_seam` returned
   `PARSER_FAILED` instead of `GEMINI_FAILED`.
   GREEN: both pass after classifying locked/deadline-exhausted initialization and
   attaching the failure's diagnostics to the final event. They check bounded
   elapsed time, zero attempts, unchanged allowances, request/operation identity,
   and persisted event/diagnostic agreement.
2. RED: `test_direct_parser_construction_rejects_equal_models_before_client_calls`
   failed because construction did not raise `ValueError`.
   GREEN: the constructor reuses the existing model configuration validator when
   a fallback is supplied. The test verifies no client calls or allowance usage.

Unrelated SQLite operational failures before the deadline continue to propagate;
initialization without an operation retains its existing behavior. Timeout
diagnostics avoid database reads while the exclusive lock is held, matching the
bounded reservation path.

## Acceptance criteria

The references below identify existing coverage plus the new corrective tests.
`policy` means `tests/test_issue_14_gemini_policy.py`, `browser` means
`tests/test_spike_contract.py`, and `v8`/`v9` mean the corresponding correction
test modules. Test names below omit the `test_` prefix.

| # | Criterion | Coverage |
| --- | --- | --- |
| 1 | Aggregate run allowance includes retries/fallback | policy: `run_allowance_counts_repeated_invocations_as_attempts`, `run_allowance_is_shared_by_multiple_operations_and_concurrent_reservations`, `rate_limit_fallback_uses_remaining_run_budget_and_different_model` |
| 2 | Persistent per-model daily allowance | policy: `daily_counter_survives_restart_and_rolls_at_configured_timezone_boundary`, `daily_exhaustion_is_an_explicit_application_outcome_without_a_provider_call` |
| 3 | Explicit timezone/day reset | Same restart test crosses midnight in `America/New_York`; policy defaults to UTC midnight. v8: `cli_gemini_policy_configuration_is_visible_on_natural_language_outcome` |
| 4 | Structured JSON uses zero Gemini calls | policy: `structured_json_path_has_zero_gemini_calls_even_when_run_budget_is_zero`; application tests in `test_issue_3_app.py` cover validation, confirmation, and execution |
| 5 | Normal natural-language parsing uses one call | policy: `natural_language_run_records_one_bounded_call_in_final_event`; deterministic validation/confirmation coverage in `test_issue_4_correction.py` |
| 6 | Deterministic browser/security checks use zero calls | browser: `deterministic_browser_paths_return_structured_zero_call_outcomes`, `run_spike_rejects_public_steel_url_before_constructing_client`, `steel_control_and_debugger_surfaces_must_be_private` |
| 7 | Bounded attempts/backoff/deadline, no SDK multiplication | policy: `transient_retry_is_bounded_and_backoff_is_observable`, `retry_backoff_cannot_outlive_operation_deadline`, sync/async timeout and reservation-contention tests; v9 real-lock setup tests; SDK retry boundary in `test_dependency_contract.py` |
| 8 | Required failure classifications/model provenance | policy: `required_provider_classifications_and_redaction_are_stable`, retry/fallback and final-outcome tests; v9 proves setup timeout classification before a model attempt |
| 9 | Eligible, distinct fallback shares budgets/deadline | policy: `rate_limit_fallback_uses_remaining_run_budget_and_different_model`, `transient_retry_reserves_a_fallback_attempt`, `fallback_daily_exhaustion_stops_without_calling_fallback`; v8 async-turn lifecycle test; v9 direct constructor rejection |
| 10 | Exhaustion returns explicit non-success without further calls | policy: `daily_exhaustion_is_an_explicit_application_outcome_without_a_provider_call`, `browser_run_budget_exhaustion_is_a_structured_application_outcome`, `final_outcome_identifies_fallback_reservation_denial` |
| 11 | Outcome/event counters and decision provenance | policy: natural-language final-event, transient-retry, fallback-denial tests; v9 checks terminal timeout diagnostics in both persisted application event and diagnostic record |
| 12 | Redact secrets/session material | policy redaction and persisted-diagnostic tests; v8 serialized sensitive-container redaction tests |
| 13 | Offline seam tests with exact call counts | policy recording providers and browser fakes cover calls, retries, fallback, persistence, deadlines, and provenance; v9 adds real SQLite contention and direct construction seams |
| 14 | Input through final application status/event | policy natural-language, structured-request, and exhaustion tests; v9 locked `run_request` test checks returned and persisted `GEMINI_FAILED` with `timeout_cancellation` |

## Verification

Use the Python 3.11 dependency environment on `PATH` as well as for the test
runner, because existing CLI tests launch `python3` subprocesses. Set
`PYTHONPATH=src` to exercise this worktree's source.

- Full suite: `python3.11 -m unittest discover -s tests -v` — 141 tests pass
  (138 inherited plus three corrective tests).
- Compilation: `python3.11 -m compileall -q src tests` — passes.
- Installed dependency consistency: `python3.11 -m pip check` — no broken requirements.
- Dependency contracts: `python3.11 -m unittest discover -s tests -p 'test_dependency_contract.py' -v` — two tests pass.
- Whitespace: `git diff --check` and staged diff check.
- Dependency manifests remain unchanged from `4647ad0` (`pyproject.toml`,
  `package.json`, `package-lock.json`).
