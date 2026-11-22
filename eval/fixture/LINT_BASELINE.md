# Fixture lint baseline

`eval/fixture/lint_baseline.json` records two `MISRA-15.1.3` findings (single-argument
constructor must be `explicit`) on the out-of-class constructor definitions in
`src/link_state_machine.cpp` and `src/ring_buffer.cpp`.

These are false positives of the lexical rule: the in-class declarations in
`include/link_state_machine.hpp` and `include/ring_buffer.hpp` already carry `explicit`,
and C++ forbids repeating `explicit` on an out-of-class definition. No rule is disabled
and no fixture code is changed (eval task patches depend on its exact content).

Regenerate with:

```bash
python3 enforcement/lint/preflight_lint.py --config eval/fixture/preflight.toml \
  --repo-root eval/fixture --baseline eval/fixture/lint_baseline.json --baseline-update eval/fixture
```

Any new finding beyond this baseline fails the gate.
