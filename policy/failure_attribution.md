# Failure attribution policy

Every failed or reworked agent run must explain **why** it failed so the team fixes the
root cause (briefs, code context, skills, harness/tools, or model routing) instead of
retrying blindly (`policy/failure_handling.md`).

## 1. Cause taxonomy

Each attributed run receives **one primary cause** and an **optional secondary cause**:

| Cause | Meaning | Typical examples |
|---|---|---|
| `BRIEF` | Input brief was insufficient, incorrect, or contradictory | Missing interface/design decision, rule contradicts grader, wrong file path or line reference, false premise, scope too large for one run, missing required heading |
| `CONTEXT` | Agent could not locate or understand relevant repository code | Many search/read calls before first edit, read wrong module, hallucinated API or symbol, re-implemented an existing helper, "not found" claims for existing code |
| `SKILL` | Domain skill or rule knowledge was wrong, missing, or failed to trigger | Skill should have triggered but did not; skill rule conflicts with project rule; recurring lint rule violation without active skill guidance |
| `TOOL` | Harness, sandbox, or tool contract failed | Permission denied for a needed tool, unclear tool contract, prompt/context truncation (`tool_protocol_errors`), lint false positive, sandbox/IO error |
| `MODEL` | Brief, context, and tools were adequate, but the model produced wrong work or gamed the gate | Failed logic/unit tests despite accurate inputs, modified protected test assertions (`test_lock` failure), scope violation |
| `ENV` | Infrastructure, quota, network, or wall-clock environment failure | API quota/rate-limit rejection, network outage, container startup failure, wall-clock timeout (excluded from quality metrics alongside `harness_error`) |

## 2. Pre-dispatch brief lint (`runner/brief_lint.py`)

Before dispatching a worker, `runner/brief_lint.py` mechanically checks the brief against
the target repository and engine budget:

- `BRIEF-HEADINGS`: required sections present (`Goal`, `Input`, `Expected output`,
  `Constraints` / `STOP conditions`, `Gate` / `Commands`).
- `BRIEF-NO-SCOPE`: non-empty allowed-files declaration (`## Files allowed`).
- `BRIEF-PATH-MISSING`: path-like tokens resolve to existing files in `--repo`.
- `BRIEF-LINE-OUT-OF-RANGE`: `file:line` or `line N` references stay within file length.
- `BRIEF-SYMBOL-MISSING`: backticked function calls or named functions/classes checked
  against the repository (emitted as `warning` only, because new symbols may be created).
- `BRIEF-TOO-LONG`: estimated tokens (`ceil(chars / 4)`) compared against the engine's
  brief budget derived from `context_tokens` in `runner/engines.example.toml`.
- `BRIEF-CONTRADICTION`: mechanical conflict between `Constraints` / `Out of scope` and
  `Expected output` (e.g., forbidding edits to a path that expected output asks to modify).

Severity depends on the author:

- **Orchestrator-written briefs (`--author orchestrator`, enforced by `runner/devpack_broker.py`):**
  structural, path, line-range, budget, scope, and contradiction findings are **errors**
  that refuse the broker submission and return the JSON findings to the orchestrator.
  Warnings (`BRIEF-SYMBOL-MISSING`) are recorded in `run.json` under `brief_lint`.
- **Human-written briefs (`--author human`):** all findings are downgraded to **warnings**
  and recorded in `run.json` under `brief_lint` without blocking dispatch.

## 3. Heuristic log analysis (`runner/devpack_logs.py analyze`)

`python3 runner/devpack_logs.py analyze <run_dir> [--repo DIR] [--engines FILE]`
computes deterministic heuristic signals (no LLM) from `run.json`, `steps/*.prompt.md`,
`steps/*.meta.json`, `steps/*.jsonl`, and `steps/*gate*.txt`, and writes
`<run_dir>/attribution.json`:

- **Signals recorded:** `tool_calls_by_kind`, `search_calls_before_first_edit`,
  `files_read`, `files_touched`, `files_read_vs_touched`, `missing_path_reads`,
  `denied_calls`, `missing_symbols` (C/C++ identifiers referenced in edits/claims that do
  not exist in `--repo`), `skill_triggers`, `prompt_tokens_est`, `context_window`,
  `prompt_over_context`, `tool_protocol_errors`, `timeout`, `quota_or_network_error`,
  `not_found_claims`, and `brief_lint_findings`.
- **Output schema (`attribution.json`):**
  `{suspected_cause, secondary, confidence ("low"|"medium"|"high"), signals {...}, evidence [{step, line, text}], analyzed_at, confirmed [...]}`.
- All evidence excerpts and paths pass through `Redactor` (`policy/logging.md`).

## 4. Human confirmation (`runner/devpack_logs.py attribute`)

Heuristic attribution is advisory until a human confirms or corrects it:

```bash
python3 runner/devpack_logs.py attribute runs/<run-id> \
  --cause BRIEF \
  --secondary CONTEXT \
  --note "Brief omitted required header signature for recoverFromFault" \
  --by "tech-lead"
```

This validates `--cause` (and optional `--secondary`) against the taxonomy and appends
`{cause, secondary, note, by, timestamp}` to `attribution.json` under `confirmed`.

## 5. Routing causes to fixes (learning loop)

Confirmed attributions drive targeted fixes rather than blind retries:

| Cause | Fix target |
|---|---|
| `BRIEF` | Update brief templates (`workflow/templates/`), `orchestrator/CLAUDE.md`, or `runner/brief_lint.py` checks |
| `CONTEXT` | Improve repository context / code index injection (`AGENTS.md`, `eval/` context arms) |
| `SKILL` | Author or refine skills via `knowledge/skills/` and validate with `skilltest/skilltest.py` |
| `TOOL` | Fix runner, broker, linter, or sandbox contracts (`runner/`, `enforcement/`) |
| `MODEL` | Update engine profiles (`knowledge/engine_profiles/`), qualification thresholds (`policy/local_models.md`), or engine/role routing |
| `ENV` | Exclude from model quality metrics; fix server context/quota/timeout configuration |

## 6. Director jobs and v0.2 build rework retrospective

Director-level orchestration jobs and `agent-hub` incident entries use the **exact same
six cause labels** (`BRIEF`, `CONTEXT`, `SKILL`, `TOOL`, `MODEL`, `ENV`) so rework across
build pipelines and eval runs is tracked under one taxonomy.

Recorded v0.2 build rework evidence (documented in `knowledge/lessons.md`):

| Lesson | Primary cause | Secondary | Evidence summary |
|---|---|---|---|
| `LSN-016` | `TOOL` | `MODEL` | Default local server context window truncated prompts and tool schemas, causing plain-text JSON tool calls (`tool_protocol_errors`) |
| `LSN-017` | `BRIEF` | - | Saturated v0.1 task briefs named exact files and bugs, hiding multi-file and spec-driven capability gaps |
| `LSN-018` | `BRIEF` | `MODEL` | Weak task graders allowed a builder agent to pass checks using empty seeded defects and marker files |
| `LSN-019` | `TOOL` | `MODEL` | Evaluation graders honoured self-authored `Deviation:` trailers in `test_lock.py` until `--no-deviation` was enforced |
