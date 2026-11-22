# ai-devpack

A thin, portable **AI development harness** for large professional automotive C/C++
projects that may use only mid- and small-size models. It gives you policy, agent
knowledge, workflow templates, deterministic checks and an evaluation kit. Everything runs as
local scripts (bash + Python 3.10+ standard library) callable from any CI.

Design principles: small closed tasks; deterministic checks before any model judgment;
CI runs the gate (agent claims are not evidence); review is one exhaustive pass with a fixed
taxonomy; at most 2 fix rounds; escalation ends at a **human**; agents never merge; sandbox
by default; provenance recorded internally.

## Layout

| Path | Content |
|---|---|
| `policy/` | operating model, ASIL boundaries (company policy, needs safety approval), sandbox, data governance, provenance, failure handling, tool confidence, deviations |
| `knowledge/` | `lessons.md`, `context_template/AGENTS.md`, skills (MISRA C++:2023 digest, review taxonomy, Doxygen/naming) |
| `workflow/` | brief/plan/ADR templates, CI pipeline description, GitLab CI and Jenkins examples |
| `runner/` | `devpack_run.py` local runner, `engines.example.toml` |
| `enforcement/` | gate, preflight lint, checks, hooks, evidence collector |
| `eval/` | fixture, tasks and runner to measure pass@1 / pass^k of the agents |
| `tests/` | repo-level tests (`test_no_project_leaks.py`, `run_all.sh`) |

## Install into a project

1. **Day 0:** fill in `policy/data_governance.md` section 4 (data owner sign-off) and get
   `policy/asil_boundaries.md` approved by the safety manager.
2. Copy or vendor the pack at a pinned version (for example as a `devpack/` directory or
   a git subtree). Do not edit pack files in the project; open a change against the pack.
3. Copy `knowledge/context_template/AGENTS.md` to the project root and replace every
   `{{PLACEHOLDER}}`.
4. Copy `enforcement/gate/gate.example.toml` to the project's gate config and list the
   project's build, test and lint commands.
5. Install `enforcement/hooks/pre-commit` as the git pre-commit hook.
6. Set up the sandbox container (`policy/sandbox.md`) and the agent credentials.
7. Copy `runner/engines.example.toml` and set the role-to-command templates.
8. Wire CI using `workflow/ci/pipeline.md` and the examples.

## Quickstart

```bash
bash tests/run_all.sh                                                    # all pack tests
python3 enforcement/gate/gate.py --config enforcement/gate/gate.example.toml   # expect GATE_OK
python3 runner/devpack_run.py --help
```

Write a brief from `workflow/templates/brief_implement.md` (the tech lead approves it), run
the task through the runner, let CI run the gate, then review, fix at most twice, and hand
to the human reviewer.

## Versioning

Semantic versioning; the current version is in `VERSION` and changes in `CHANGELOG.md`.
Projects pin a version and compare it in CI (LSN-011).

## Limits

- Standards statements in `policy/` are marked "not verified against the standard text".
- External claims carry a URL and the mark "external, verify".
- The ASIL table is a proposal and needs safety approval before use.

## License

See `LICENSE` (proprietary placeholder).
