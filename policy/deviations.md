# Deviations

Every gate bypass, exemption, skipped check, disabled hook, baseline entry, or sandbox
relaxation is a **deviation**. Silent bypass is a process violation (LSN-001).

## Register

Human-authorized deviations live **outside agent-writable worktrees** in a human/broker-managed registry file referenced by `DEVPACK_DEVIATIONS` (TOML or JSON; e.g. a broker-owned path on the host). Each entry records:

| Field | Required |
|---|---|
| `id` | `DEV-<nnn>` (unique deviation identifier) |
| `files` | List of relative file paths / globs covered by the deviation |
| `rules` | **Required** non-empty list of rule/check IDs covered (e.g. `test_lock`, `PROCESS-TERMINATION`, `SUPPRESSION-WITHOUT-DEVIATION`, `max_fix_rounds`); rule-less entries are rejected |
| `approved_by` | Non-empty human approver identifier (TL / safety manager for safety-relevant items) |
| `expires` | ISO-8601 date (`YYYY-MM-DD`) or timestamp; expiry at most 90 days |
| `reason` | Why the requirement cannot be met now and compensating measure |

Example TOML registry (`DEVPACK_DEVIATIONS=/etc/devpack/deviations.toml`):

```toml
[[deviations]]
id = "DEV-001"
files = ["eval/fixture/tests/test_can_codec.cpp"]
rules = ["test_lock"]
approved_by = "tech-lead"
expires = "2026-12-31"
reason = "Replace legacy assertion signature per REQ-CAN-012"
```

Keep tracking details in `deviations/DEV-<nnn>.md` or your ticket system if desired; enforcement tools honour only entries verified in `DEVPACK_DEVIATIONS`.

## Rules

1. **Agent commits never self-authorize deviations.** `runner/devpack_dispatch.py` and
   `runner/devpack_run.py` always pass `--no-deviation` to `enforcement/checks/test_lock.py`.
   Commit-message `Deviation: <ID>` trailers are ignored for agent-produced runs.
2. When an operator invokes `test_lock.py`, `preflight_lint.py`, or `devpack_run.py --max-fix-rounds > 2`
   with an explicit `--deviation <ID>`, the check requires `DEVPACK_DEVIATIONS` to exist outside
   the worktree and match by `id`, affected `files`, `rules`, non-empty `approved_by`, and
   non-expired `expires`. Missing, unconfigured, inside-worktree, mismatched, or expired entries
   fail closed.
3. Expired deviations make the gate fail.
4. Agents may *propose* a deviation in an escalation note (`STOP_REPORT.md`); only humans add
   entries to `DEVPACK_DEVIATIONS`.
5. Deviations are reviewed at each release gate; open ones are listed in the release notes.
6. A baseline of known findings counts as a deviation per entry, not per file.
