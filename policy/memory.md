# Agent Memory Policy

Agents start fresh on every job with no conversation carry-over. Continuity across
jobs comes from an explicit, bounded, auditable project memory store managed by
`runner/memory.py` and `runner/devpack_memory.py`.

## 1. Store layout and separation from engine profiles

Memory entries live under `.devpack/memory/<scope>/<id>.md` as Markdown files
with YAML frontmatter (`id`, `type`, `scope`, `tags`, `created`, `source`,
`supersedes`, `expires`, `status`).

- **Project scope** (`scope = project` and `module:<path>`) is human-readable
  and git-tracked when shared across the team.
- **Role and engine scopes** (`role:<role>`, `engine:<name>`) restrict entries
  to matching execution contexts.
- **Engine profiles are separate.** Model-specific failure patterns and coding
  template reminders belong in `knowledge/engine_profiles/` (see
  `knowledge/engine_profiles/README.md`), not in project memory.

## 2. What may be stored

Permitted entry types:

| Type | Purpose | Approval |
|---|---|---|
| `decision` | Architectural or design choices affecting future tasks | Human approval (`approve`) required |
| `fact` | Verified repository facts or interface constraints | Human approval (`approve`) required |
| `handoff` | Bounded task-completion summary for follow-on work | Auto-accepted after gate passes |
| `known-gap` | Unresolved gap or blocker recorded in `STOP_REPORT.md` | Auto-accepted after gate passes |

### Forbidden content (secret and PII rejection)

Memory must never store secrets, credentials, API tokens, passwords, private
keys, personal data (emails, personal names, host home directory paths), or
customer-restricted material that violates `policy/data_governance.md`.

Every memory write (human CLI `add`, `approve`, or runner proposal acceptance)
runs `enforcement/checks/secret_pii_check.py` built-in rules, configured
external patterns (`DEVPACK_SECRET_PATTERNS`), credential/token regexes, and
PII/home-path rules **before** writing to disk. Rejected entries are never
written and error messages never echo the matched secret.

## 3. Write path and who approves

Agents never write `.devpack/memory/` directly:

1. **Agent proposal:** an agent may append a `## Memory proposals` section to
   its final output (or `STOP_REPORT.md`).
2. **Post-gate processing:** `runner/devpack_dispatch.py`, `runner/devpack_run.py`,
   and `runner/devpack_broker.py` parse proposals **only after** the
   deterministic gate (`gate_cmd`, `test_lock.py`, `scope_check.py`,
   `preflight_lint.py`) passes:
   - `handoff` and `known-gap` entries are stored as `active`.
   - `decision` and `fact` entries are stored as `pending` and are **never
     injected** into prompts until a human reviews and runs:
     `python3 runner/devpack_memory.py approve <id>`.
3. **Human management:** humans inspect and maintain memory using
   `runner/devpack_memory.py` (`list`, `show`, `add`, `approve`, `supersede`,
   `expire`, `query --explain`).

## 4. Retrieval, bounds, and scope isolation

`runner/agent_exec.py` performs deterministic stdlib retrieval (no embeddings)
controlled by `[memory]` in `runner/engines.example.toml` (`enabled`, `top_k`
default `5`, `budget_tokens` default `800`, `reviewer_sees_handoffs` default
`false`):

- Matches task files, modules, REQ IDs, and symbols against entry `tags` and
  `scope`.
- Drops `pending`, `superseded`, and `expired` entries before ranking.
- Ranks by specificity first, then recency, and stops at `top_k` and
  `budget_tokens` to prevent over-recall.
- Enforces scope isolation: `role:<R>` entries are visible only to role `R`,
  `engine:<E>` entries only to engine `E`, and `reviewer` steps do not receive
  implementer `handoff` entries unless explicitly configured.
- Records injected `memory_ids`, `memory_hashes` (SHA-256), and
  `memory_tokens` in each step's `.meta.json` (see `policy/logging.md` and
  `policy/provenance.md`).

## 5. Retention

- `handoff` and `known-gap` entries should carry an `expires` timestamp
  (recommended retention: 30 to 90 days) and can be swept with
  `python3 runner/devpack_memory.py expire --expired`.
- `decision` and `fact` entries remain `active` until replaced via
  `supersedes` / `supersede <old-id>` or explicitly expired.
- Superseded entries remain on disk for auditability with `status: superseded`
  and are never injected into prompts.

## 6. Stale-memory rule (authoritative precedence)

**Current repository code and specifications always win.**

Memory entries are advisory summaries from earlier work and may become stale.
Every `## Relevant project memory` prompt block explicitly instructs the agent
that current source code and specifications take precedence on any conflict, and
that any detected contradiction between memory and code/spec must be reported
explicitly rather than silently followed.

See `eval/memory_tasks` for the regression suite verifying recall, supersede
handling, distractor resistance, stale-memory conflict reporting, and scope
isolation.
