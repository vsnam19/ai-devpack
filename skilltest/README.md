# skilltest

`skilltest` validates compact Claude Code skills and compares behaviour with and without a skill. It uses only the Python standard library and `runner.agent_exec` for real trials.

```sh
python3 skilltest/skilltest.py lint knowledge/skills/doxygen-naming
python3 skilltest/skilltest.py run knowledge/skills/doxygen-naming --engines haiku,sonnet --k 3 --dry-run
python3 skilltest/skilltest.py run knowledge/skills/doxygen-naming --engines haiku,sonnet --k 3
```

Results are written below `skilltest/results/<UTC-date>/<skill>/`. Each trial has a shared-layout run directory, and `summary.md` plus `summary.json` record the experiment. Do not commit results.

## Configuration

Place `tests/skilltest.toml` under the skill directory.

```toml
[[trigger]]
name = "write documentation"
prompt = "add Doxygen comments to this declaration"
expect = true

[[behaviour]]
name = "documentation is complete"
fixture = "fixtures/missing-docs/input"
grader = "graders/missing-docs"
prompt = "repair the fixture"
check = "bash check.sh"
```

Use at least two true and two false `[[trigger]]` entries and one `[[behaviour]]` entry. `fixture` is relative to `tests/` and names an `input/` directory copied into an isolated work directory for each behaviour arm. `grader` names a separate directory that is never copied; the check runs from that directory and receives the work directory as its LAST argument. Bash graders must use `"${@: -1}"` to select it, including when the command has preceding arguments. Exit 0 means pass. Store a corrected fixture under `tests/expected/` and verify in a unit test that the check fails on the supplied fixture and passes on the expected output.

Trigger evidence is a stream-json `Skill` tool call that names the skill, or a `Read` tool call for `.claude/skills/<name>/SKILL.md`. The report computes pass-rate delta (`with_skill - without_skill`) and flags no measurable benefit when the delta is zero or negative.

Select engine definitions with `--engines-config FILE` (default: `runner/engines.example.toml`). Engine names resolve to their configured model, class, endpoint environment variable names, timeout, and context requirements. Only non-test skill assets are installed; `tests/` is excluded.
