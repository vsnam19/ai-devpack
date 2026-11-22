# Skill checklist

Before shipping a skill, confirm:

1. Frontmatter has `name` equal to the directory and a short `description` saying what and when.
2. The body is under 200 lines, uses imperative steps, and has one worked example.
3. References use relative links and optional detail lives outside `SKILL.md`.
4. No absolute paths, credentials, secrets, or personal data appear.
5. Each instruction has an observable result or a deterministic check.

## Skill test configuration template

```toml
[[trigger]]
name = "positive request"
prompt = "perform the skill job"
expect = true

[[trigger]]
name = "negative request"
prompt = "perform an unrelated job"
expect = false

[[behaviour]]
name = "output meets rule"
fixture = "fixtures/input"
prompt = "edit the fixture to meet the skill rule"
check = "bash check.sh ."
```

Supply two positive and two negative trigger entries. The fixture must initially fail its check. Store a corrected copy under the tests/expected/ directory; add a unit test that proves the check passes there.
