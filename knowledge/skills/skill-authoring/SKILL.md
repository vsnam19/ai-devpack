---
name: skill-authoring
description: Write or improve an agent skill when a workflow needs reliable weak-model execution; use for skill requests, trigger design, skill tests, and SKILL.md reviews.
---

# Skill authoring

Write for the weakest intended model. A skill is a short, executable instruction sheet, not background reading.

1. Interview the human: identify the job, trigger phrases, inputs, safe outputs, stop conditions, and deterministic check.
2. Give the skill directory the same name as frontmatter `name`.
3. Keep the description under 1024 characters. Say what it does and when to use it; include natural trigger words.
4. Keep the body at 200 lines or fewer. Use direct imperative steps, ordered workflow, concrete commands, and explicit failure handling.
5. Put optional detail in `references/` and link it relatively. Never include absolute paths, credentials, tokens, or personal data.
6. Make every rule observable: pair it with an output, command, or check.
7. Add the skill test configuration before declaring the skill ready. Include at least two prompts that should trigger, two that should not, and one behaviour case.
8. Run `python3 skilltest/skilltest.py lint <skill-dir>`. Then inspect `python3 skilltest/skilltest.py run <skill-dir> --engines haiku,sonnet --dry-run`; ask before a paid run.

## Worked example

For a `release-notes` skill, describe: "Draft release notes from merged changes; use when asked for release notes, changelog, or announcement." The body tells the agent to read change records, group user-visible changes, state unknowns, and produce Markdown. Positive prompts include "write release notes" and "prepare a changelog". Negative prompts include "fix a parser" and "review this patch". Its behaviour fixture contains incomplete notes; the check requires a heading and an `Unknowns` section.

See [the checklist](references/checklist.md) for the review and test-file template.
