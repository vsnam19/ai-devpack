# Sources and Verification Status

This file records provenance and verification status for external claims informing `requirements-analysis`. Secondary summaries and preprints are marked `verify` and must not be cited to customers as verified standard clauses without checking the licensed text.

## Standards and industry guides

1. **Automotive SPICE (SYS.2, SWE.1, SWE.6)**
   - VDA QMC Guidelines 2.0 (PAM 4.0) `[src: https://vda-qmc.de/wp-content/uploads/2023/06/BGB_A-SPICE-Guidlines-2.0_V8.pdf — verify]` (guidelines reference; official PAM text not read).
   - VDA QMC Guidelines 2.1 (PAM 4.1, March 2026) `[src: https://vda-qmc.de/wp-content/uploads/2026/03/ASPICE_BGB_2.1_Yellow-Volume_final-2.pdf — verify]` (title/snippet only).
   - UL SWE.1 summary (BP1–BP8 paraphrased, and distinction between traceability link existence and semantic consistency) `[src: https://www.ul.com/sis/insights/software-requirements-analysis-swe1-automotive-spice — verify]` (page fetched; UL wording, not PAM text).
   - UL Automotive SPICE Pocket Guide (SYS.2 snippet) `[src: https://www.ul.com/sites/default/files/2024-10/Automotive_Spice_Pocket_Guide.pdf — verify]` (PDF fetch failed; snippet only; exact SYS.2 BP numbers `[unsourced — verify]`).
   - SWE.6 expected qualification-test traceability content `[unsourced — verify]`.

2. **ISO 26262 Part 8 §6 and Part 6**
   - ISO 26262-8:2011 Clause 6 title ("Specification and management of safety requirements") `[src: https://standards.iteh.ai/catalog/standards/iso/6ebe2c1d-91cc-4c1b-abd1-c789f519e27b/iso-26262-8-2011 — verify]`.
   - ISO 26262-8:2018 TOC (6.4.2 attributes/characteristics, 6.4.3 management) `[src: https://cdn.standards.iteh.ai/samples/68390/8b6b8ddbf06b436e8ac60f72f63c17d9/ISO-26262-8-2018.pdf — verify]` (snippet only).
   - Secondary paraphrases of safety requirement properties: Siemens white paper `[src: https://resources.sw.siemens.com/cs-CZ/white-paper-intelligent-requirements-traceability-for-iso-26262 — verify]` and SAE paper `[src: https://saemobilus.sae.org/content/2015-01-0265 — verify]`; attributes (operating modes, ASIL, safe state) `[unsourced — verify]`.
   - ISO 26262-6 unit-testing methods (equivalence classes, boundary values for ASIL B–D, fault injection; table numbering varies across vendor summaries) `[src: https://www.embitel.com/?p=6860 — verify]`, `[src: https://www.parasoft.com/learning-center/iso-26262/unit-testing/ — verify]`, and combined boundary/equivalence claim `[unsourced — verify]`.

3. **INCOSE Guide for Writing Requirements (GtWR) & ISO/IEC/IEEE 29148**
   - INCOSE GtWR v4 summary sheet (INCOSE-TP-2010-006-04, July 2023; C1–C10 characteristic list; C4/C10 numbering discrepancy noted) `[src: https://www.incose.org/docs/default-source/working-groups/requirements-wg/guidetowritingrequirements/incose_rwg_gtwr_v4_summary_sheet.pdf — verify]` (snippet only), `[src: https://arxiv.org/pdf/2401.16330 — verify]` (not read), `[src: https://www.incose.org/wp-content/uploads/legacy/presentation/IS2023-pdf/INCOSEContent-411.pdf — verify]` (snippet only).
   - ISO/IEC/IEEE 29148 nine quality characteristics used as evaluation rubric in Lubos et al. 2024 `[src: https://arxiv.org/html/2408.10886v1 — verify]`.

4. **EARS (Easy Approach to Requirements Syntax)**
   - Six EARS patterns and keywords (`WHEN`, `WHILE`, `WHERE`, `IF`, `THEN`, `SHALL`) `[src: https://en.wikipedia.org/wiki/Easy_Approach_to_Requirements_Syntax — verify]` (Wikipedia; original Mavin et al., RE'09 paper not read).

## Empirical LLM studies on requirements analysis (2023–2026)

- **Lubos et al., 2024** (ISO 29148 assessment with Llama 2 70B on Stopwatch and DigitalHome): high recall (0.74–1.0) but very low precision on real project requirements (0.1257 independent on DigitalHome), showing high false-alarm rates `[src: https://arxiv.org/html/2408.10886v1 — verify]`.
- **Bashir et al., ICSME 2025** (in-context learning on three industrial datasets): 10-shot prompting improved ambiguity classification by 20.2% over zero-shot `[src: https://www.es.mdu.se/publications/7221-Requirements_Ambiguity_Detection_and_Explanation_with_LLMs__An_Industrial_Study — verify]` (listing only).
- **Poulsen et al., 2026** (246 NASA JWST requirements + 246 ambiguous variants): project context reduced false positives on well-written requirements `[src: https://www.designsociety.org/publication/49144/context-aware_large_language_models_for_ambiguity_detection_in_requirements — verify]` (listing only).
- **arXiv 2609.03230 (2026 preprint)** ("Two Truths and A Lie?"): best Anthropic model achieved 47% median true-positive rate and 11% median false-alarm rate against INCOSE criteria; necessity and correctness issues were almost always missed; authors recommend human-in-the-loop decision support rather than autonomous judgment `[src: https://arxiv.org/abs/2609.03230 — verify]` (abstract read; peer review unverified).
- **arXiv 2608.21298 (2026 preprint)** (crossover experiment, 34 participants): LLM support reduced smell-detection accuracy and learning effect `[src: https://arxiv.org/abs/2608.21298 — verify]` (abstract read).
- **Additional unverified / snippet-only references:** ClarifySTL `[src: https://arxiv.org/pdf/2605.01209 — verify]`, LMAdetect `[src: https://crad.ict.ac.cn/en/article/pdf/preview/10.7544/issn1000-1239.202550700.pdf — verify]`, Krishna et al. 2024 `[unsourced — verify]`, Fantechi et al. `[unsourced — verify]`, Mahbub et al. `[unsourced — verify]`.

## Open items (must be closed before claiming standards compliance)

1. Obtain and verify licensed **ISO 26262-8:2018** clause 6 exact property and attribute lists.
2. Obtain and verify licensed **ISO 26262-6:2018** unit-verification method tables and ASIL recommendation levels.
3. Obtain and verify licensed **VDA Automotive SPICE PAM 4.0 / 4.1** base-practice texts and numbering for SYS.2, SWE.1, and SWE.6.
4. Read the full **INCOSE GtWR v4 (2023)** text to confirm C1–C15 numbering and definitions.
5. Read the primary **Mavin et al. (RE'09)** EARS paper.
6. Read full PDFs of **arXiv 2609.03230**, **arXiv 2608.21298**, Mahbub et al., Krishna et al., and Fantechi et al.
