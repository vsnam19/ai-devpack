# Pipeline operating modes

Status: draft v0.1. Owner: engineering lead of the adopting project.

The ai-devpack pipeline (`plan -> briefs -> implement -> gate -> review -> fix -> ready-to-merge`,
plus memory decisions, deviations, and escalations) supports two operating modes configured in
`.devpack/project.toml` under `[pipeline]`:

1. **`hitl` (human-in-the-loop, default)** — Every blocking checkpoint pauses execution until a
   human operator inspects and approves or rejects it via `runner/devpack_approve.py`. Terminal
   checkpoints write pending approval records when the run finishes without altering the run's
   existing exit code. When `[pipeline]` is omitted, `hitl` is the default.
2. **`autonomous`** — Auto-approvable checkpoints are approved automatically with
   `decided_by = "autonomous-policy"` **only** when deterministic gate/lint evidence on disk
   shows the stage checks passed. A red or missing gate receipt keeps the checkpoint `PENDING`.
   Hard-stop checkpoints **always** require human approval in every mode. Autonomous mode still
   never merges and never weakens a gate (`policy/operating_model.md`).

## 1. Checkpoints

Each checkpoint is classified along two axes:

- **Control-flow kind (`blocking` vs `terminal`):**
  - **Blocking (`plan`, `brief`, `deviation`, `selinux-manifest`):** Work cannot continue until
    decided. If the checkpoint evaluates to `PENDING` (in `hitl`, on a hard-stop, or on a red
    gate), the runner stops with exit code `20` (`AWAITING_APPROVAL`), writes
    `<state_dir>/approvals/pending/<uuid>.json`, and persists continuation state (job spec,
    brief SHA-256 hash, checkpoint ID) in `<state_dir>/runs/<uuid>.json`. Once approved with
    `runner/devpack_approve.py approve <uuid>`, the run continues via:
    - `runner/devpack_run.py --resume <uuid>` (for `devpack_run` workflows),
    - `runner/devpack_dispatch.py --resume <uuid>` (for direct `devpack_dispatch` jobs), or
    - `runner/devpack_dispatch.py --submit --resume <uuid>` / `runner/devpack_broker.py --resume <uuid>`
      (for host broker jobs).
    Resuming verifies that the brief file is unchanged (SHA-256 match) and consumes the
    approval atomically so it can run at most once; edited briefs or second resume attempts are
    refused.
  - **Terminal (`pre-merge`, `escalation`, `memory-decision`):** Recorded when the run finishes.
    A terminal checkpoint writes an approval record (pending when human action is required) and
    **never changes the run's existing exit code** (a ready change still exits `0`, awaiting
    merge approval; an escalation keeps exit code `1` / `ESCALATE_TO_HUMAN`).

| ID | Stage | Kind | What is decided | Who approves in `hitl` | What `autonomous` does | Classification |
|---|---|---|---|---|---|---|
| `plan` | Plan | `blocking` | Task plan before drafting briefs | Tech lead (TL) | Auto-approves with green plan/gate evidence; otherwise `PENDING` | Auto-approvable |
| `brief` | Brief | `blocking` | Each brief before worker dispatch | Tech lead (TL) | Auto-approves when `runner/brief_lint.py` reports 0 error-severity findings; otherwise `PENDING` | Auto-approvable |
| `deviation` | Deviation | `blocking` | New deviation registration | TL / Safety manager (`policy/deviations.md`) | Auto-approves non-safety (QM / ASIL A) and non-security deviations with green evidence; **hard-stop** (`PENDING`) when safety-relevant (ASIL >= B per `policy/asil_boundaries.md`) or security-relevant | Conditional hard-stop |
| `selinux-manifest` | Domain / SELinux | `blocking` | SELinux policy/manifest change | Security / data owner | Auto-approves non-release builds with green gate evidence; **hard-stop** (`PENDING`) for release builds | Conditional hard-stop |
| `pre-merge` | Ready to merge | `terminal` | Change ready for merge after gate + review | Human reviewer (`policy/operating_model.md`) | Auto-records `APPROVED` when merging into a known non-protected `--target-branch` and gate + review passed (still never merges); **hard-stop** (`PENDING`) when target branch is protected (`main`, `master`, `release/*`, `hotfix/*`) or unknown | Conditional hard-stop |
| `escalation` | Escalation | `terminal` | Action after 2 failed fix rounds or STOP condition | Tech lead (TL) | Always writes `PENDING` approval record; keeps `ESCALATE_TO_HUMAN` (exit `1`) | Hard-stop |
| `memory-decision` | Memory | `terminal` | Promotion of `decision` / `fact` memory proposals | Human reviewer (`policy/memory.md`) | Always writes `PENDING` approval record; keeps existing exit code | Hard-stop |

The plan checkpoint applies during planning before briefs are drafted. When
`devpack_plan.py run-next` starts a task from an existing plan, it delegates to the
task runner and applies its brief and terminal checkpoints. A blocking pause
returns exit code 20 and holds the plan until the attempt is resumed or rejected.
Plan validation checks structure and brief lint before task selection.

## 2. Configuration and tightening rules

Configure mode and state location in trusted `.devpack/project.toml`:

```toml
[pipeline]
mode = "hitl"                 # "hitl" (default) | "autonomous"
# state_dir defaults to $XDG_STATE_HOME/devpack/<project-hash> (outside the worktree).
# If set explicitly, it must live outside the worktree and any writable worker mount.
# state_dir = "/var/lib/devpack/state/my-project"
target_branch = "main"        # merge target checked by pre-merge (defaults to first protected branch or "main")
protected_branches = ["main", "master", "release/*", "hotfix/*"]

[pipeline.checkpoints]
# Optional per-checkpoint overrides ("auto" | "human"); may only make policy stricter.
# Setting a hard-stop (e.g. escalation, memory-decision) to "auto" is rejected.
plan = "human"
```

Rules enforced by `runner/checkpoints.py`:

1. **Default is `hitl`.** Absent `[pipeline]` or omitted `mode` selects `hitl`.
2. **Tightening-only overrides:**
   - `[pipeline.checkpoints]` cannot set a hard-stop checkpoint (`escalation`, `memory-decision`)
     to `"auto"`, and cannot set `"auto"` when `[pipeline].mode` is `"hitl"`. Invalid configs
     fail closed.
   - CLI `--mode` on `runner/devpack_run.py` and `runner/devpack_dispatch.py`, as well as
     `DEVPACK_MODE`, may only tighten (`autonomous` -> `hitl`), never loosen (`hitl` -> `autonomous`).
3. **Trusted `state_dir`:**
   - Resolved from trusted pre-run/host configuration, defaulting to
     `$XDG_STATE_HOME/devpack/<project-hash>` (outside the worktree and any writable worker
     mounts such as in-worktree `runs/` or log directories), never from agent-writable
     working-tree edits.
   - Any `state_dir` resolving inside the worktree, `agent_worktree`, or any writable mount is
     refused at startup, and Docker mount lists never include `state_dir`.
4. **Target-branch `pre-merge` protection:**
   - `pre-merge` evaluates the explicit merge destination (`--target-branch`, `DEVPACK_TARGET_BRANCH`,
     or `[pipeline].target_branch`, defaulting to the first entry of `protected_branches` or
     `"main"`), never only the current source branch. Unknown or empty target branches fail
     closed as protected (`hard_stop = true`).

## 3. Approval CLI, sandbox isolation, and provenance

- **Human/CI command only:** `python3 runner/devpack_approve.py <list|show|approve|reject>` is
  restricted to human operators and CI. It refuses execution inside agent sandboxes
  (`DEVPACK_AGENT_SANDBOX=1`), and `runner/devpack_broker.py` refuses any request that attempts to
  invoke or forward `devpack_approve`.
- **Identity redaction:** Approver identity is recorded as a non-PII SHA-256 digest of the OS
  user name (`human:<12hex>`), and all checkpoint payloads are scrubbed via `runner/redaction.py`.
- **Unified memory approval format:** Pending `decision`/`fact` (and quarantined) memory proposals
  from `runner/memory.py` create `memory-decision` pending records in `<state_dir>/approvals/pending/`.
  Approving via either `runner/devpack_approve.py` or `runner/devpack_memory.py` keeps both stores
  synchronized.
- **Provenance:** Every decision (`autonomous-policy` or human `approve`/`reject`) is appended to
  `<state_dir>/approvals/provenance.jsonl` and, when a git repository with `HEAD` is present, to
  `refs/notes/ai-provenance` (`policy/provenance.md`).
