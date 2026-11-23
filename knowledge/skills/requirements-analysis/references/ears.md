# EARS Patterns (Easy Approach to Requirements Syntax)

> Source summary: `[src: https://en.wikipedia.org/wiki/Easy_Approach_to_Requirements_Syntax — verify]` (secondary summary; original Mavin et al., RE'09 paper not read directly).
> Limit: EARS constrains sentence structure, not content `[unsourced — verify]`. A valid EARS sentence can still be defective if its response uses vague terms (`"SHALL respond quickly"`) or invented numbers. Always use `<TBD: ...>` placeholders for values missing from the source document.

## Clause order

Optional preconditions -> optional trigger -> `<system name>` -> `SHALL` -> `<system response>`.

Keywords: `WHEN`, `WHILE`, `WHERE`, `IF`, `THEN`, `SHALL`.

## The six EARS patterns

1. **Ubiquitous** (always active; no keyword)
   - Template: `The <system name> SHALL <system response>.`
   - Example: `The CanCodec SHALL operate without dynamic heap allocation.`

2. **Event-driven** (`WHEN`)
   - Template: `WHEN <trigger>, the <system name> SHALL <system response>.`
   - Example: `WHEN startConnect(nowMs) is called while state() is Idle, the LinkStateMachine SHALL transition to Connecting.`

3. **State-driven** (`WHILE`)
   - Template: `WHILE <precondition(s)>, the <system name> SHALL <system response>.`
   - Example: `WHILE isFrozen() is true, the DtcDebouncer SHALL leave counter() unchanged on reportSample() calls.`

4. **Optional feature** (`WHERE`)
   - Template: `WHERE <feature is included>, the <system name> SHALL <system response>.`
   - Example: `WHERE extended CAN addressing is enabled, the CanCodec SHALL accept 29-bit identifiers.`

5. **Unwanted behaviour** (`IF` / `THEN`)
   - Template: `IF <unwanted trigger or fault>, THEN the <system name> SHALL <system response>.`
   - Example: `IF no heartbeat arrives within <TBD: heartbeat_timeout_ms> ms, THEN the LinkStateMachine SHALL enter Degraded.`

6. **Complex** (`WHILE` + `WHEN`)
   - Template: `WHILE <precondition(s)>, WHEN <trigger>, the <system name> SHALL <system response>.`
   - Example: `WHILE state() is Healing, WHEN reportSample(true) is called, the DtcDebouncer SHALL reset healingCounter() to 0 and transition to Confirmed.`

## Rewrite discipline

- Keep the original requirement ID (or `TMP-n` if unnumbered).
- Use `SHALL` for mandatory behaviour; split compound `SHALL ... and ...` clauses into separate atomic EARS statements.
- Copy numeric values only if they appear in the source specification; replace every missing duration, threshold, tolerance, or error code with `<TBD: descriptive_name_and_unit>`.
