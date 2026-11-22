# CI pipeline

CI-neutral. Each stage is a local script call that any CI can run. Example wiring:
`examples/gitlab-ci.yml`, `examples/Jenkinsfile` (examples only).

## Stage order

| # | Stage | Runs | Pass condition | On failure |
|---|---|---|---|---|
| 1 | preflight | `python3 enforcement/lint/preflight_lint.py <changed files>`, `enforcement/checks/secret_pii_check.py`, sandbox self-check | exit 0 | STOP, nothing sandboxed starts |
| 2 | implement (sandboxed) | `python3 runner/devpack_run.py --task <brief> ...` in the container | brief handoff produced | classify per `policy/failure_handling.md` |
| 3 | gate | `python3 enforcement/gate/gate.py --config <file>` **in CI, never the agent's claim** | `GATE_OK` and receipt JSON | go to fix round |
| 4 | review | reviewer role, read-only, one exhaustive pass | last line `VERDICT: APPROVE` | `VERDICT: REQUEST_CHANGES` -> fix round |
| 5 | fix (max 2 rounds) | fixer role, then gate and review again | gate OK and APPROVE | after round 2: human |
| 6 | human | independent human reviewer, safety manager if ASIL C-D | explicit approval | merge by CI job after approval |

Evidence (`enforcement/evidence/collect.py`) is bundled after stages 3-5 and kept with the
change. Provenance notes are written by the runner/CI (`policy/provenance.md`).

## Rules

1. The gate in CI is the only truth for "checks pass". An agent saying "tests pass" is not evidence.
2. Agents and orchestrators never merge. The merge job needs a human approval.
3. Stage timeouts are configured; a timeout is a failure.
4. Fix rounds <= 2; no model escalation; the final escalation is a human.
5. A missing tool in the gate environment is FAIL (fail closed).
6. Pin the pack version (`VERSION`) in the project and compare in CI.
7. Heavy builds are limited per host (LSN-006); scripts take their own locks, callers do not wrap them.
