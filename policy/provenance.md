# Provenance of AI-produced changes

Provenance is recorded **internally** for every change that an AI produced or materially
changed. Removing all traces of AI use from history destroys information needed for audits
and incident analysis (LSN-013). Whether and how to **disclose** AI use to a customer is a
separate decision of the owner (see `data_governance.md` section 5) and is not made by this
record or by agents.

## What is recorded

| Field | Meaning |
|---|---|
| `model` | Model family name as configured in the engine file |
| `model_version` | Exact model identifier returned by the service |
| `prompt_hash` | SHA-256 of the brief/prompt file used (hash only; the prompt itself is stored in the evidence bundle) |
| `input_commit` | Commit SHA the agent started from |
| `task_id` | Task identifier |
| `role` | implementer / fixer / reviewer |
| `gate_receipt` | Path or hash of the gate receipt |
| `human_reviewer` | Role id of the approving human (no personal data in git notes) |
| `timestamp` | UTC, ISO 8601 |

## Where

1. **Git notes** on the resulting commit, ref `refs/notes/ai-provenance`:
   `git notes --ref=refs/notes/ai-provenance add -m '<json>' <commit>`.
   The ref is pushed explicitly (`git push origin refs/notes/ai-provenance`) and fetched by CI.
   Notes are not rewritten by rebase; the merge job re-attaches them to the final commit SHA.
2. **Evidence bundle** `evidence/<change-id>.json` produced by
   `enforcement/evidence/collect.py`: gate receipt, review verdict, provenance note, diff stat.

## Rules

- The note is written by the runner/CI, not by the agent.
- Commit messages stay clean (no tool trailers are required); provenance lives in notes and evidence.
- Retention: same as the project's safety records.
- A secret/PII scan covers notes and evidence.
- Background reading on AI authorship provenance (external, verify):
  https://attested-delivery.github.io/docs/concepts/09-ai-authorship-provenance/ and
  https://docs.kernel.org/process/coding-assistants.html
