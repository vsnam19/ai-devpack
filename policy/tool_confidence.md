# Tool confidence worksheet (AI use cases)

> **Not verified against the standard text.** This worksheet follows the structure of
> ISO 26262 part 8 clause 11 (tool impact TI, tool error detection TD, tool confidence
> level TCL) as summarised in secondary sources (external, verify:
> https://www.itemis.com/en/glossary/tool-qualification/). The safety manager must check
> every statement against the standard before relying on it.

One worksheet per AI use case (for example "implementer drafts unit tests for QM code").

## Method (simplified)

1. Define the use case and the work product it can influence.
2. **TI** (tool impact): TI1 if a tool malfunction cannot introduce or fail to detect an
   error in a safety-related item; otherwise TI2. A coding agent that writes production
   code is TI2.
3. **TD** (tool error detection): how likely is an error introduced by the tool to be
   prevented or detected downstream? TD1 = high confidence of detection, TD2 = medium, TD3 = other.
   **TD1 may only be claimed from measures that are independent of AI**: compiler with
   warnings as errors, static analysis, the project's tests written or reviewed by humans,
   structural coverage measured by tools, independent human review. An AI reviewer is
   *not* an independent measure for this purpose, because it may share failure modes
   with the AI author.
4. **TCL** = result of TI/TD combination from the standard's table (look up in the
   standard; do not take from this file). TCL1 needs no qualification; TCL2/TCL3 need
   qualification measures chosen by the safety manager.
5. For AI/ML tools assume a worse TD until evidence shows otherwise (external,
   verify: https://www.renesas.com/en/blogs/renesas-fusa-support-automotive-4-confidence-use-software-tools-aiml-development).

## Worksheet template

| Field | Entry |
|---|---|
| Use case ID | UC-{{N}} |
| Description | |
| ASIL of affected items | |
| Work products influenced | |
| Possible tool errors (examples: wrong logic, hidden weakened test, hallucinated API, incomplete requirement coverage) | |
| TI and reasoning | |
| Detection measures (list each) | |
| Is each measure independent of AI? (yes/no per measure) | |
| TD and reasoning | |
| TCL (from standard table) | |
| Qualification measures if TCL > 1 | |
| Usage constraints (model, version, gate config version) | |
| Approved by (role), date | |

## Pre-filled starting positions (proposal, not approvals)

| Use case | Start TI | Start TD | Note |
|---|---|---|---|
| Agent drafts comments/Doxygen | TI1 or TI2 | TD2 | Human review required |
| Agent drafts unit tests (QM) | TI2 | TD2 | Test-lock + mutation or coverage evidence help |
| Agent writes production code (QM, ASIL A-B) | TI2 | TD2 unless independent measures justify TD1 | Gate + independent human review |
| Agent writes production code (ASIL C-D) | TI2 | TD3 | Not used unless safety manager approves an argument |

Related: `asil_boundaries.md`, `deviations.md`.
