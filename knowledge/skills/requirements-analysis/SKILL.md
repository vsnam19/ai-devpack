---
name: requirements-analysis
description: Analyse automotive system or software requirement sets for defects, ambiguity, gaps, conflicts, missing values, and missing traceability, producing a findings table, clarification questions, and bounded test ideas without inventing numbers. Use when asked to review requirements, check spec testability, audit protocol or state-machine specs, or derive requirement-based test cases.
---

# Requirements analysis

> Working checklist anchored on INCOSE / ISO 29148 vocabulary. Standard clause references in `references/` are secondary summaries (`verify` against licensed standards before citing to a customer). Never claim standard compliance.

## When to use

Use when reviewing system or software requirements (for example CAN, protocol, or state-machine specs), checking testability, auditing traceability, or deriving requirement-based test cases. Do not use for C++ implementation, MISRA code review, or release notes.

## Workflow

1. **Inventory every requirement first.**
   Read the entire specification before judging any item. List every requirement with its ID. If a requirement has no ID, assign `TMP-1`, `TMP-2`, ... in document order and state explicitly in the report that temporary IDs were assigned.

2. **Check each requirement** against [the quality checklist](references/checklist.md):
   - **Atomic / singular:** one requirement, one behaviour; flag compound statements joined by `"and"`, `"or"`, or `"as well as"`.
   - **Unambiguous:** flag every term from [the ambiguity lexicon](references/ambiguity-lexicon.md).
   - **Verifiable:** a concrete pass/fail criterion can be written.
   - **Complete:** states units, tolerances, valid ranges, timing (nominal, timeout, retry count), preconditions/initial state, and error or degraded behaviour.
   - **Conforming syntax:** follows [EARS patterns](references/ears.md) (`WHEN`, `WHILE`, `WHERE`, `IF/THEN`, `SHALL`).
   - **Attributes and trace links:** parent/child links, status, and ASIL as given (never raise or invent an ASIL).

3. **Check the requirement set as a whole:**
   - **Conflicts:** same trigger or condition with different responses, thresholds, or timings.
   - **Duplicates or overlaps:** overlapping rules that should be merged or clarified.
   - **Gaps:** missing modes or states, unhandled transitions, missing error paths (invalid or missing input, out-of-range value, timeout, link loss, reset), and missing upstream/downstream trace links (check link existence separately from semantic consistency).
   - **Open items:** `TBD`, `TBC`, `to be defined`, `xx`, or `???`.

4. **Write the findings report** to the path requested by the user (default `review/findings.md` when asked to write a findings file).
   Include a findings table with this exact header:

   `| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |`

   Table rules:
   - `ID`: requirement ID from the source (or `TMP-n` when missing).
   - `defect type`: checklist rule / lexicon category plus confidence (`[high]`, `[medium]`, `[low]`) and `candidate`.
   - `quote`: verbatim text from the source document; never paraphrase.
   - `why it matters`: verification, safety, or implementation impact.
   - `question to stakeholder`: specific question asking for the missing unit, range, timeout, state, or rule.
   - `proposed rewrite`: EARS form using placeholders such as `<TBD: timeout ms>` for every missing value; **use only numeric values that already appear in the source document**.

   After the table, include:
   - `## Set-level gaps and conflicts` (cross-requirement conflicts, state/error gaps, trace gaps).
   - `## Open questions` (numbered list for stakeholder sign-off).
   - `## Derived test ideas` (next step).

5. **List derived test ideas** (equivalence classes, boundary values, state transitions, timing, error injection) **only for requirements with no open questions**. For any requirement with open findings or missing values, record `blocked — waiting on <ID> question` instead of inventing test values.

## Stop rules

- **Never invent values.** Never put a number (timing, range, tolerance, threshold, retry count, error code, ASIL) into a proposed rewrite or test idea unless that number appears in the source document. Use `<TBD: ...>` placeholders.
- **Never drop a requirement.** Account for every inventoried requirement; flag every defective item in the table, and do not add fake findings to clean requirements.
- **Never judge correctness or compliance.** Flag defects as candidates with confidence (`high`, `medium`, `low`) and ask the stakeholder; do not declare a requirement "correct" or "standard-compliant".

## Worked example

Source line:
`REQ-EX-01: The controller shall respond quickly after a bus timeout.`

Findings row:
```markdown
| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |
|---|---|---|---|---|---|
| REQ-EX-01 | Ambiguous term + missing timing/error path [high, candidate] | "The controller shall respond quickly after a bus timeout." | "quickly" is untestable; bus timeout duration, tolerance, and recovery state are unspecified. | What is the bus timeout in ms, its tolerance, the maximum response time in ms, and the target fault state? | IF no valid bus frame is received within <TBD: bus_timeout_ms> ms, THEN the controller SHALL transition to <TBD: fault_state> within <TBD: response_ms> ms. |
```

Derived test ideas entry:
`- REQ-EX-01: blocked — waiting on REQ-EX-01 stakeholder answers (missing timeout and fault state).`

## References

- [Quality checklist](references/checklist.md) — Q1–Q15 checks with definitions, fail signals, and examples.
- [Ambiguity lexicon](references/ambiguity-lexicon.md) — vague words and missing-quantity patterns to flag.
- [EARS patterns](references/ears.md) — sentence templates for proposed rewrites.
- [Sources and verification status](references/sources.md) — research provenance and open standard-verification items.
