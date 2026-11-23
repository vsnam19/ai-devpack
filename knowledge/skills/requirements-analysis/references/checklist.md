# Requirement Quality Checklist

> Working checklist anchored on INCOSE Guide for Writing Requirements (GtWR) and ISO/IEC/IEEE 29148 vocabulary. ISO 26262-8 and Automotive SPICE (SYS.2 / SWE.1) references below are secondary summaries (`verify` against licensed standards before quoting to a customer). See [sources.md](sources.md).

## Single-requirement checks (Q1–Q15)

| # | Defect type | Definition & anchor | Fail signal | Example |
|---|---|---|---|---|
| Q1 | Missing or duplicate ID | Every requirement has a unique, stable identifier (ISO 26262-8 attribute list, paraphrase — verify). | No ID, or same ID used twice. | Unnumbered: `"The codec shall clear buffers on boot."` -> assign `TMP-1` and flag. |
| Q2 | Non-atomic (not singular) | States one requirement only without unrelated clauses (INCOSE C5 Singular `[src: summary sheet — verify]`). | `"and"`, `"or"`, `"as well as"` joining distinct behaviours. | `"The node shall drop bad frames and log a warning and reset the timer."` |
| Q3 | Unnecessary / unlinked need | Traces to a parent requirement or stakeholder need (INCOSE C1 Necessary — verify). | No upstream trace ID, or unrequested behaviour. | `"The ECU shall blink a status LED"` with no parent `SYS-*` requirement. |
| Q4 | Wrong abstraction level | Fits the target level (system vs software) without premature design (INCOSE C2 Appropriate — verify). | System req names a C++ mutex or register bit. | `"SYS-04: The system shall allocate a std::array of size 8."` |
| Q5 | Ambiguous wording | Allows one interpretation only (INCOSE C3 Unambiguous — verify). | Words in [ambiguity-lexicon.md](ambiguity-lexicon.md). | `"The module shall respond quickly after reset."` |
| Q6 | Incomplete specification | States trigger, precondition, response, timing, units, tolerance, range, initial state (INCOSE C10 / ISO 29148 Complete `[src: https://arxiv.org/html/2408.10886v1 — verify]`). | Number without unit (`50`), missing range, or missing initial state. | `"IF no frame arrives within 50, THEN enter Degraded."` (`50` has no unit). |
| Q7 | Infeasible constraint | Achievable within known hardware, timing, or architecture budgets (INCOSE C6 Feasible — verify). | Violates stated bus rate, memory, or cycle budget. | `"Transmit 100 CAN frames per 1 ms on a 500 kbps bus."` |
| Q8 | Unverifiable | A concrete pass/fail test or inspection criterion can be written (INCOSE C7 Verifiable; SWE.1 BP5 — verify). | Subjective quality claim (`"user-friendly"`, `"acceptable"`). | `"The diagnostic output shall be clear and reliable."` |
| Q9 | Inconsistent with parent | Accurately refines its parent requirement (INCOSE C8 Correct; SWE.1 BP6/7 consistency — verify). | Contradicts parent threshold, unit, or mode. | Parent says `100 ms` timeout; child software req says `200 ms`. |
| Q10 | Non-conforming syntax | Follows the project template or EARS pattern (INCOSE C9 Conforming; [ears.md](ears.md) — verify). | Narrative prose without actor, trigger, or `SHALL`. | `"Heartbeat monitoring happens in the background."` |
| Q11 | Missing error / degraded behaviour | Defines response to invalid input, timeout, link loss, or fault where faults can occur (ISO 26262 practice — verify). | Nominal case only; no out-of-range, `nullptr`, or timeout path. | `"Decode frame 0x210 to degC"` with no DLC/CRC/range error handling. |
| Q12 | Implementation-biased | Avoids dictating an algorithm, library, or variable unless required by constraint (ISO 26262-8 property list, paraphrase — verify). | Names internal helper or library unnecessarily. | `"The debouncer shall use a HashMap to store counters."` |
| Q13 | Missing status or ASIL | Safety/project attributes (status, ASIL) are stated and inherited without unapproved edits (ISO 26262-8 attributes — verify). | Missing `Status` or `ASIL` on safety req, or child lowers/raises ASIL silently. | Safety requirement with `ASIL: TBD` or missing status. |
| Q14 | Missing traceability link | Bidirectional links exist: upstream parent and downstream verification reference (SWE.1 BP6/7 `[src: UL summary — verify]`). | Orphan requirement or no linked test criterion. | `"REQ-09"` has no `Traces to` tag and no test reference. |
| Q15 | Set-level conflict | Does not contradict any other requirement in the set (INCOSE set consistency — verify). | Same trigger/condition, different response, threshold, or timing. | `REQ-11` confirms fault at counter `100`; `REQ-12` confirms at `50`. |

## Set-level checks

1. **Traceability vs consistency:** Confirm both that a link exists (traceability) and that linked content agrees semantically (consistency). `[src: https://www.ul.com/sis/insights/software-requirements-analysis-swe1-automotive-spice — verify]`
2. **State and mode coverage:** For every state-driven requirement, check every other defined state for the same event; flag unhandled `(state, event)` pairs as gaps.
3. **Error and boundary coverage:** For each input/interface, check invalid value, missing value, out-of-range, timeout, link loss, and reset; for each numeric range, check min, max, and out-of-bounds behaviour.
4. **Timing completeness:** For every timed behaviour, check nominal period/duration, unit, tolerance, retry count, back-off, and expiry action.
5. **Open items:** Collect all `TBD`, `TBC`, `xx`, and `???` items into the stakeholder open-questions list.
