# ASIL boundaries for AI-assisted work

> **Company policy - NEEDS SAFETY APPROVAL.** This table is a proposal by the pack
> authors. It is not a statement of what ISO 26262 requires. Standards statements below
> are *not verified against the standard text*. The safety manager must approve or
> amend this file before adoption.

Applies to the whole pack. "AI" means any code- or text-producing model or agent.
In all classes, **AI never merges** (see `operating_model.md`).

| Activity | QM | ASIL A-B | ASIL C-D |
|---|---|---|---|
| Draft production code (implementer, small task) | Allowed; gate + review + human merge | Allowed; gate + review + independent human reviewer; extra review depth set by safety manager | **Not allowed by default.** Production code is written by humans. Exception only if the safety manager approves a tool confidence argument (`tool_confidence.md`) for that use case; record as deviation. |
| Draft unit tests | Allowed | Allowed; humans review that test intent matches requirement | Allowed as a draft; human owns test design and review of test adequacy |
| Test-lock rule (no weakening of existing tests) | Enforced | Enforced | Enforced |
| Doc, comments, Doxygen, renames without behaviour change | Allowed | Allowed with human review | Allowed with human review |
| Refactoring | Allowed | Allowed with regression evidence | Humans only (same rule as production code) |
| Review assistance (reviewer role) | Allowed | Allowed as an additional reviewer, never the only reviewer | Allowed as an additional reviewer only; does not replace the independent human confirmation review |
| Requirements, architecture, safety analysis (FMEA, FTA, HARA) | Drafting aid only, TL owns | Drafting aid only; human authors | Humans author; AI only as a checklist aid with all output re-derived by the human |
| Static-analysis rule fixes | Allowed | Allowed with review | Humans only unless approved as above |
| Generated code accepted without human reading | Not allowed | Not allowed | Not allowed |

## Notes

1. **Independence.** Confirmation measures and review independence depend on the ASIL;
   the exact levels are not verified here. The human reviewer must not be the
   author of the brief for the same change at ASIL B and above (proposal).
   Reference on independence terminology (external, verify):
   https://www.exida.com/Blog/iso-26262-independence-and-related-terms
2. **Structural coverage.** Do **not** state that MC/DC is mandatory at ASIL C. Public
   vendor sources differ: they agree MC/DC is highly recommended at ASIL D and are unclear
   or inconsistent for ASIL C (external, verify:
   https://www.rapitasystems.com/iso26262 and
   https://www.verifysoft.com/en_ISO_26262_Road_Vehicles_Functional_Safety.html).
   The project's software verification plan sets the coverage target; this pack does not.
3. **Tool confidence.** Use of AI to produce code is a software tool use case in the
   sense of ISO 26262 part 8 clause 11. See `tool_confidence.md`. Detection (TD1) must
   come from measures that are independent of AI.
4. **AI-specific standards.** ISO/PAS 8800 concerns AI in road vehicles; its scope
   statement indicates it does not give specific guidelines for software tools that use AI
   methods (external, verify: https://www.iso.org/standard/83303.html and
   https://www.ul.com/sis/blog/safety-related-systems-road-vehicles-artificial-intelligence-are-addressed-isopas-88002024).
   Do not cite it as a qualification route for coding agents without checking the text.
5. **MISRA.** A public vendor statement says generated code from LLMs is not treated as
   "automatically generated code" under MISRA C:2025 (external, verify:
   https://www.qt.io/quality-assurance/axivion/misra-check?hsLang=ko-kr). Treat
   AI output as hand-written code that needs the same rule compliance and deviations.
6. A study of LLM output against MISRA C++:2008 found no model produced fully compliant
   code (external, verify: https://arxiv.org/abs/2506.23535). The gate, not the model, decides
   compliance.
7. Classification of a file into QM / ASIL is read from the project's safety
   documentation, never guessed by an agent. If the class is unknown, treat as ASIL C-D.
