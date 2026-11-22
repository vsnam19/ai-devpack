# MISRA C++:2023 themes digest

Not verified against the standard text. Themes and checks, not rule texts. Use the licensed
standard for exact wording and numbering.

| Theme | Agent action | Typical check |
|---|---|---|
| Memory | No heap after init; no placement tricks; bounded containers | Lint: `new`/`delete`/`malloc` detection |
| Casts | `static_cast` only; no const removal; no pointer-integer casts | Lint: C-style cast pattern |
| Conversions | Explicit narrowing; same-signedness comparisons; no implicit bool from pointer where the analyser objects | Compiler `-Wconversion`, analyser |
| Control flow | Complete `switch`; no `goto`; single exit preferred; loops bounded | Compiler `-Wswitch-enum`, lint |
| Initialisation | Initialise at declaration; member initialiser lists in declaration order | Compiler `-Wuninitialized`, `-Wreorder` |
| Classes | Rule of 0/3/5 respected; `explicit` ctors; `override`; no virtual call in ctor/dtor | Lint, analyser |
| Templates and lambdas | No default captures across threads; no unnecessary template metaprogramming | Review taxonomy CPX |
| Error handling | Check return values; `[[nodiscard]]`; no exceptions when disabled | Compiler `-Wunused-result`, lint |
| Preprocessor | Avoid function-like macros; include guards; no `#undef` tricks | Lint |
| Libraries | No `<cstdio>`/`std::cout` in runtime paths; project logging only; no `<cstdlib>` heap functions | Lint |
| Concurrency | Document thread ownership; no blocking calls on event-loop thread; shared state guarded | Review |
| Naming/Doxygen | See `knowledge/skills/doxygen-naming/SKILL.md` | Lint |

## Deviations

A documented deviation names the rule theme, location, reason, risk, approver and expiry; see
`policy/deviations.md`. Agents may propose, never approve.
