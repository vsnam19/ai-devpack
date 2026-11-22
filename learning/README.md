# Learning Loop (`learning/`)

Mines run logs for recurring mistakes, drafts bounded per-engine profiles, and validates
whether a candidate profile reduces first-attempt lint findings without regressing pass
rate before a human promotes it.

## Workflow

1. **Harvest** (`learning/harvest.py`):
   Reads run directories (`runs/<run-id>/`) — step `.meta.json`, gate/lint logs, review
   tables, tool protocol errors, and STOP reports — and aggregates recurring failures by
   `(engine, rule_id)` with redacted example snippets.
   Writes `learning/reports/<date>.md` and `learning/reports/<date>.json`.

   ```bash
   python3 learning/harvest.py runs --since 2026-11-01
   ```

2. **Propose** (`learning/propose.py`):
   Reads a harvest JSON report and drafts a candidate profile for one engine from the
   top-N recurring findings at or above `--min-count`, capped at `<= 40` lines. Never
   overwrites active profiles in `knowledge/engine_profiles/`.

   ```bash
   python3 learning/propose.py --harvest learning/reports/2026-11-23.json --engine local
   ```

3. **Validate** (`learning/validate.py`):
   Compares the active profile against the candidate profile via A/B evaluation on the
   `smoke` tier (and optionally tasks where findings occurred), or offline on recorded
   `results.json` pairs. Accepts **only** if `lint_findings_first_attempt` strictly
   drops and pass rate does not regress. Writes a decision report and prints the exact
   human promotion commands on `ACCEPT`.

   ```bash
   python3 learning/validate.py --engine local --candidate learning/reports/candidate-local-v1.md --k 3
   ```

## Guardrails

- `learning/reports/` is git-ignored.
- No tool ever auto-edits `knowledge/engine_profiles/`, `knowledge/skills/`, or `policy/`,
  and no tool ever auto-commits.
- All harvested snippets are redacted with `runner.agent_exec.redact_sensitive_text`.
