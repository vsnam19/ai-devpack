# Ambiguity Lexicon

> Proposed list based on common requirements engineering practice `[unsourced — verify]`. Anchor formal reviews on INCOSE GtWR C3 (Unambiguous) and ISO/IEC/IEEE 29148. Flag every match, quote it verbatim, and ask the stakeholder for a measurable rule or number.

## 1. Speed and quality adjectives

- **Words:** `fast`, `quick`, `quickly`, `rapid`, `rapidly`, `slow`, `efficient`, `robust`, `reliable`, `user-friendly`, `intuitive`, `high quality`, `good`, `acceptable`, `appropriate`, `appropriately`, `adequate`, `sufficient`, `suitable`, `normal`, `typical`, `clean`, `clear`.
- **Why it matters:** Subjective adjectives cannot yield a deterministic pass/fail test criterion.
- **Ask:** What measurable quantity (in ms, bytes, error rate, or exact output state) defines this behaviour?

## 2. Quantifier and boundary vagueness

- **Words:** `some`, `several`, `many`, `most`, `few`, `multiple`, `minimal`, `minimum` (without value), `maximum` (without value), `approximately`, `about`, `around`, `roughly`, `etc.`, `and so on`.
- **Why it matters:** Leaves equivalence-class boundaries and buffer/loop limits undefined.
- **Ask:** What are the exact lower and upper bounds, inclusive/exclusive limits, and units?

## 3. Hidden or relative timing

- **Words:** `immediately`, `instantly`, `in time`, `soon`, `as soon as possible`, `eventually`, `periodically` (without period), `after a delay`, `at startup` (without phase/event).
- **Why it matters:** Real-time and protocol code requires explicit deadlines, periods, jitter tolerances, and start/stop measurement events.
- **Ask:** What is the exact duration, unit (ms/us/cycles), tolerance, and from which start event to which end event is it measured?

## 4. Escape clauses

- **Words:** `where practical`, `if practical`, `if feasible`, `if possible`, `as needed`, `unless otherwise specified`, `except as needed`, `may` (instead of `shall`).
- **Why it matters:** Allows an implementation to skip required behaviour without failing verification.
- **Ask:** Under which exact precondition or operating mode is this behaviour mandatory vs omitted?

## 5. Vague subjects and missing actors

- **Patterns:**
  - `"the system"` or `"the module"` when multiple subcomponents have distinct responsibilities.
  - `"the user"` when operator, technician, and diagnostic tester roles differ.
  - Passive voice with no actor (`"errors are handled"`, `"the frame is dropped"`, `"counters are reset"`).
- **Why it matters:** Leaves interface ownership and module boundaries ambiguous.
- **Ask:** Which named component or interface performs the action?

## 6. Missing units, tolerances, or ranges

- **Patterns:** Any bare number (`within 50`, `threshold 10`), scale without range, or physical quantity without unit (`degC`, `ms`, `V`, `bit`).
- **Why it matters:** Unit mismatches (`ms` vs `ticks`, `degC` vs `K`) and missing saturation bounds cause silent safety bugs.
- **Ask:** What is the exact unit, numeric range `[min, max]`, resolution, and tolerance?

## 7. Open placeholders

- **Patterns:** `TBD`, `TBC`, `TBR`, `to be defined`, `to be determined`, `xx`, `???`, `[insert ...]`.
- **Why it matters:** Requirements with unresolved placeholders cannot be verified or implemented safely.
- **Ask:** Who owns closing this open item and by which milestone?
