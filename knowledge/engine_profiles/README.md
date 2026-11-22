# Engine Profiles

Per-engine guidance profiles (`knowledge/engine_profiles/<engine>.md`) capture recurring
mistakes observed in run logs and state the concrete correction to apply instead.

## Format

Each profile file follows this plain-Markdown structure:

```markdown
# Engine Profile: <engine>
Version: <integer>

- [<RULE-ID>] (<N> occurrences): <known mistake> -> <do this instead>
```

## Rules

1. **Maximum 40 lines (`<= 40` lines total).** Every profile file including its header and
   comments must stay at or below 40 lines so weak and local models do not lose prompt
   budget to profile bloat.
2. **Version header required.** `Version: <N>` is an integer starting at `0` for an empty
   stub and incremented by `1` each time a human promotes an accepted candidate.
3. **Rule ID and occurrence citations required.** Every bullet item must cite the exact
   rule ID or failure category (for example `DOXYGEN-PARAM-DIR`, `NO-IMPL-IN-HEADER`,
   `MISRA-8.2.2`, `TOOL-PROTOCOL-ERROR`) and the occurrence count from `harvest.py`.
4. **Human-in-the-loop promotion.** Tools (`propose.py`, `validate.py`) never edit active
   files under `knowledge/engine_profiles/` or auto-commit. A human inspects the A/B
   validation report and commits the promoted profile.
5. **Hot reload and provenance.** `runner/agent_exec.py` reads the active profile from
   disk on every invocation and records `profile_sha256` and `profile_version` in the step
   `.meta.json` artifact.

## Example

Start from `knowledge/engine_profiles/local.md` (Version `0`, empty list).
