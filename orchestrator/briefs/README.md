# Worker Briefs (`orchestrator/briefs/`)

Each file in `briefs/` defines **one closed worker job** dispatched via `runner/devpack_dispatch.py`.

## Required sections

1. **Title & Role** — Task ID and short title.
2. **Goal** — One concise paragraph describing the exact behaviour or requirement IDs.
3. **Input** — Files to read (headers, source, spec sections).
4. **Expected output** — Concrete artifacts, signatures, and test expectations.
5. **Files allowed** — Exact POSIX globs matching `--allow` passed to `devpack_dispatch.py`.
6. **Commands** — Deterministic build/test commands for verification.
7. **STOP conditions** — When to stop and write `STOP_REPORT.md` instead of guessing or widening scope.

## Automatic checklist injection

At every dispatch, `runner/devpack_dispatch.py` reads `runner/quality_checklist.md` from disk and appends it to the brief sent to the worker. You do not need to duplicate the checklist manually in each brief.

## Example

See `example_implement.md` in this directory for a complete template.
