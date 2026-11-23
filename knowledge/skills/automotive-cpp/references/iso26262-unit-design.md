# ISO 26262-6 Software Unit Design and Implementation Principles

> **Verify against licensed standard.** The ISO 26262-6:2018 standard text is paywalled and was NOT read directly. The table and properties below are compiled from public secondary sources that disagree on several ASIL ratings. Confirm every rating against ISO 26262-6:2018 Table 6 (clause 8.4.5) and the project safety plan before enforcing ASIL-specific thresholds.

For pack rules on when AI agents may write or review code per ASIL class, see `policy/asil_boundaries.md` (if the safety class is unknown, treat as ASIL C-D and stop).

---

## 1. Unit design principles (secondary source table — verify against licensed standard)

Source: NVIDIA process documentation quoting ISO 26262-6 Table 6 (clause 8.4.5) `[src: https://nvidia.github.io/spark-process/process/tracing/iso_26262/iso26262.html — verify against ISO 26262-6:2018]`.

Rating symbols (inferred from context; not defined on the quoted page `[unsourced — verify]`):
- `++` = highly recommended
- `+` = recommended
- `o` = not recommended / no recommendation

| Principle (quoted by source) | ASIL A | ASIL B | ASIL C | ASIL D | Conflict / verification note |
|---|---|---|---|---|---|
| One entry and one exit point in subprograms and functions | ++ | ++ | ++ | ++ | Conflict with secondary KB below (lists B/C/D for no multiple exits, C/D for one return). Verify against ISO 26262-6:2018. |
| No dynamic objects or variables, or else online test during their creation | + | ++ | ++ | ++ | Conflict with secondary KB below (`o / o / + / ++ / ++` for QM/A/B/C/D). Verify against ISO 26262-6:2018. |
| Initialization of variables | ++ | ++ | ++ | ++ | Consistent with secondary KB ("all ASILs"). Verify against ISO 26262-6:2018. |
| No multiple use of variable names | ++ | ++ | ++ | ++ | Verify against ISO 26262-6:2018. |
| Avoid global variables or else justify their usage | + | + | ++ | ++ | Secondary KB lists "no global shared variables (A and above)". Verify against ISO 26262-6:2018. |
| Restricted use of pointers | + | ++ | ++ | ++ | Verify against ISO 26262-6:2018. |
| No implicit type conversions | + | ++ | ++ | ++ | Secondary KB lists "no implicit type conversion (B and above)". Verify against ISO 26262-6:2018. |
| No hidden data flow or control flow | + | ++ | ++ | ++ | Verify against ISO 26262-6:2018. |
| No unconditional jumps | ++ | ++ | ++ | ++ | Consistent with MISRA `goto` prohibition. Verify against ISO 26262-6:2018. |
| No recursions | + | + | ++ | ++ | Conflict with secondary KB below (`o / o / + / ++ / ++` for QM/A/B/C/D; third snippet ties to ASIL B+). Verify against ISO 26262-6:2018. |

---

## 2. Recorded ASIL-rating conflicts across secondary sources

A separate public knowledge-base page `[src: https://notebook-dg9.pages.dev/kb/embedded/asil/sw-development/ — verify]` reports different ratings and thresholds:

| Item | NVIDIA page (A / B / C / D) | Secondary KB (QM / A / B / C / D) | Status |
|---|---|---|---|
| No dynamic objects / runtime dynamic allocation | `+ / ++ / ++ / ++` | `o / o / + / ++ / ++` (architecture: C and D normative) | **Conflict — verify against ISO 26262-6:2018** |
| No recursion | `+ / + / ++ / ++` | `o / o / + / ++ / ++` (architecture: B and above; third snippet cites ASIL B+ stack-depth link) | **Conflict — verify against ISO 26262-6:2018** |
| One entry and one exit point | `++ / ++ / ++ / ++` | Detailed design: no multiple exit points (B, C, D); one return per function (C, D) | **Conflict — verify against ISO 26262-6:2018** |
| Use of language subsets (e.g., MISRA) | Not in Table 6 excerpt | `o / + / + / ++ / ++` | **Verify against ISO 26262-6:2018** |

---

## 3. Additional detailed-design and architecture properties (unverified secondary summary)

Reported on `[src: https://notebook-dg9.pages.dev/kb/embedded/asil/sw-development/ — verify against ISO 26262-6:2018]`:
- **Detailed design properties:** no dead code (all ASILs); initialized variables at declaration (all ASILs); bounded loops (A and above); no implicit type conversion (B and above); no multiple exit points (B, C, D); one return per function (C, D).
- **Architecture properties:** defined data flow and no global shared variables (A and above); control flow analyzable, no `goto`, limited function pointers, and limited use of recursion (B and above); no dynamic object creation (C and D, normative).

Because the pack enforces the stricter baseline (`preflight_lint.py` disallows dynamic allocation, exceptions, RTTI, `goto`, and C-style casts across all C++17 files by default), follow the skill's enforced and reviewer rules uniformly unless the project safety plan and a recorded deviation state otherwise.
