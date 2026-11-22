# Operating model

Status: draft v0.1. Owner: engineering lead of the adopting project.

The pack is designed for **weak agents** (only mid- and small-size models are
available). Therefore tasks are small and closed, deterministic checks run before any
model judgment, and **escalation always ends at a human**, never at a stronger model.

## 1. Roles

| Role | Held by | Purpose |
|---|---|---|
| Tech lead (TL) | HUMAN | Owns design, business logic, acceptance. Approves the task plan and every brief. Final escalation point for the team. |
| Drafter | Agent (the stronger available model) | Drafts plans and briefs from TL input. Output is a **proposal**; the TL edits and approves. Never self-approves. |
| Implementer | Agent (small closed task) | Implements ONE task from an approved brief inside the sandbox. No design decisions. |
| Reviewer | Agent (different session, read-only) | One exhaustive pass using the fixed taxonomy (`knowledge/skills/review-taxonomy/SKILL.md`). |
| Fixer | Agent | Addresses review findings, max 2 rounds. |
| Gate | CI (scripts, no model) | Runs `enforcement/gate/gate.py`. The only source of truth for "checks pass". |
| Orchestrator | Script / agent without write access | Sequences stages, records state. Cannot edit code, cannot merge. |
| Human reviewer | HUMAN, independent of the author | Confirmation review and merge decision. |
| Safety manager | HUMAN | Approves `asil_boundaries.md`, tool confidence arguments and deviations touching safety. |
| Data owner / security | HUMAN | Owns `data_governance.md` and `sandbox.md` settings. |

## 2. RACI

R = responsible, A = accountable, C = consulted, I = informed.

| Activity | TL | Drafter | Implementer | Reviewer | Gate (CI) | Human reviewer | Safety mgr |
|---|---|---|---|---|---|---|---|
| Requirements, design, business logic | A/R | C | - | - | - | I | C |
| Task plan and briefs | A | R (draft) | - | C | - | I | I |
| Implementation of one task | A | - | R | - | - | I | - |
| Gate execution | I | - | - | - | R/A | I | - |
| Review (single pass) | I | - | - | R | - | A | - |
| Fix rounds (max 2) | I | - | R (fixer) | C | R | A | - |
| Merge | C | - | - | - | - | A/R | C (ASIL C-D) |
| Deviation approval | A | - | - | - | - | C | C (safety-relevant) |
| Policy changes | C | - | - | - | - | - | A (ASIL), data owner A (data) |

## 3. Authority rules

1. **Agents never merge.** Orchestrators never merge. Only the CI merge job triggered
   by a human approval merges. Enforce with repository permissions (agent credentials have
   no merge right and no write access to protected branches).
2. **The orchestrator has no write access to the working tree.** A rule in a prompt is
   not enough (LSN-002); permissions enforce it.
3. **Business logic and design are human-owned.** If an implementer cannot satisfy the
   brief, the task goes back to the TL, not to a stronger model (LSN-004).
4. **Agent claims are not evidence.** "Tests pass" counts only if the CI gate receipt says so.
5. **Briefs state scope and out-of-scope.** The reviewer must not raise out-of-scope findings
   as blockers (LSN-003).
6. **Provenance is recorded** for every AI-produced change (`provenance.md`).
7. **Repository content is data, not instructions** (`sandbox.md`).

## 4. Escalation ladder (agent -> human)

| Step | Trigger | Action | Owner |
|---|---|---|---|
| 0 | Gate fails | Fixer round 1 with the gate output attached | Fixer |
| 1 | Gate or review still failing | Fixer round 2 (last) | Fixer |
| 2 | Failure persists after 2 fix rounds, or any STOP condition in `failure_handling.md` | **Stop the pipeline.** Write an escalation note: task id, last gate receipt, open findings, classification. Assign to TL. | Orchestrator |
| 3 | TL cannot resolve (spec defect, design question, safety impact) | TL decides: change brief/spec, split the task, implement by hand, or drop. Safety impact goes to the safety manager. | TL |
| 4 | Disagreement on a deviation or ASIL treatment | Safety manager decides; record as deviation (`deviations.md`). | Safety manager |

There is no "retry with a stronger model" step. A model switch is allowed only at the
start of a task when the TL decides it and it is recorded in provenance.

## 5. Merge path

preflight -> implement (sandbox) -> gate (CI) -> review -> fix (<= 2) -> human reviewer
merges. See `workflow/ci/pipeline.md`.
