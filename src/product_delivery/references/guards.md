# Transition Guards

Every state transition is gated by one or more guards. A transition proceeds only when all mandatory guards return `pass` or an authorized `waived`. This document matches the guards implemented in `engine.py`.

## Guard Outcomes

| Outcome | Meaning |
|---|---|
| `pass` | Guard condition is satisfied |
| `fail` | Guard condition is not satisfied; transition blocked |
| `not_applicable` | Guard skipped via config override |
| `waived` | Guard failed but was explicitly waived with actor and reason |

## Phase Guards (implemented)

| Transition | Guards |
|---|---|
| `intake → discover` | `brief_who_populated`, `brief_problem_populated`, `project_type_determined` |
| `discover → frame` | `discover_complete` — greenfield: core_interaction + ≥1 AC; evolution: `health-check` or `maturity-assessment` evidence |
| `frame → plan` | `scope_confirmed`, `user_confirmation` — requires `user-confirmation` or `synthesis` evidence |
| `plan → deliver` | `plan_exists` (work items + plan artifact or plan evidence), `criteria_mapped` (every WI has ≥1 AC) |
| `deliver → release` | `work_items_complete`, `at_least_one_accepted`, `no_items_blocked`, `tests_pass` (suite `test-results` with exit_code 0) |
| `release → stabilize` | `coherence_check_passes` — `coherence-check` or `eval-results` evidence |
| `stabilize → closed` | `user_confirms` (`user-confirmation` evidence), `final_test_pass` (suite tests) |

## Work-Item Guards (implemented)

| Edge | Guards |
|---|---|
| `ready → implementing` | `dependencies_met` |
| `implementing → verifying` | `implementation_evidence` — item has ≥1 linked evidence key |
| `verifying → reviewing` | `tests_executed` — passing `test-results` after item entered `implementing` |
| `reviewing → accepted` | `regression_tests_pass` — passing suite `test-results` after entering `reviewing` |

`cancelled`, `rework`, and `blocked` item edges are not guard-gated.

## Evidence

Use `workflow_evidence_record` / `workflow evidence add` or `workflow_evidence_test` / `workflow evidence test`. Cursor hooks auto-record test runner shell output as `test-results` when a workflow exists.

## Overriding Guards

1. **Force**: `force=True` on transition tools / `--force` on CLI. Logged as `forced: true`.
2. **Waiver**: `workflow_waive_guard` / `workflow waive <guard>`.
3. **Config**: `guard_overrides` in `.workflow/config.json` (`warn` or `skip`).
