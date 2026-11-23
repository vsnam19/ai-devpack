# Lessons learned

Each lesson: **Incident** (generic wording) -> **Rule** -> **Enforced by** -> **Status**.
Status `enforced` = a mechanism fails the pipeline; `prompt-only` = relies on agent/human
discipline and needs a mechanism later. Mechanism paths are relative to the pack root.

## LSN-001 Gate must be tested and fail-closed
- **ID:** LSN-001
- **Incident:** A broken or ignored gate let defective changes through; it was the largest single failure.
- **Rule:** The gate is itself unit-tested; any error, timeout or missing tool is FAIL. Bypass is a deviation with a ticket.
- **Enforced by:** `enforcement/gate/gate.py`, `enforcement/gate/tests/`, `policy/deviations.md`
- **Status:** enforced

## LSN-002 Rules in prompts did not stop the orchestrator from writing code
- **ID:** LSN-002
- **Incident:** The orchestrator wrote code despite a rule forbidding it.
- **Rule:** The orchestrator has no write access; only CI merges. Authority is given by permissions.
- **Enforced by:** repository and credential permissions (`policy/operating_model.md` section 3, `policy/sandbox.md`)
- **Status:** prompt-only (until the adopting project configures the permissions; verify in the sandbox self-check)

## LSN-003 Review rounds never converged
- **ID:** LSN-003
- **Incident:** Reviewers released findings piecemeal; each round found new issues.
- **Rule:** Reviewer lists ALL findings in ONE exhaustive pass using the fixed taxonomy; brief states scope and out-of-scope; rounds are capped, then a human decides.
- **Enforced by:** `workflow/templates/brief_review.md` (last line `VERDICT:` parsed by `runner/devpack_run.py`), round cap in `engines.example.toml`
- **Status:** enforced

## LSN-004 Weak implementer could not satisfy the reviewer, even after model escalation
- **ID:** LSN-004
- **Incident:** Escalating to a stronger model did not resolve the loop; the problem was design and business logic left to the agent.
- **Rule:** Business logic and design are human-owned. Escalation ends at a human, not a stronger model.
- **Enforced by:** `policy/operating_model.md` escalation ladder; runner stops after max rounds
- **Status:** enforced (runner stop) / prompt-only (design ownership in briefs)

## LSN-005 Agents retried blindly on environment failures
- **ID:** LSN-005
- **Incident:** Agents repeated the same failing command many times when the environment (memory, disk, locks) was the cause.
- **Rule:** Classify each failure (own change / pre-existing / environment / spec defect); retry environment failures once after waiting; cap fix attempts.
- **Enforced by:** `policy/failure_handling.md`, retry caps in `runner/devpack_run.py`
- **Status:** enforced (caps in runner) / prompt-only (classification)

## LSN-006 Parallel heavy builds froze the host; a nested lock wrapper deadlocked a worker
- **ID:** LSN-006
- **Incident:** Several heavy builds at once froze the host; an outer lock wrapped around a script that locks internally deadlocked a worker for about 19 minutes.
- **Rule:** Resource limits per container; scripts own their locks (callers never wrap them); every stage has a timeout.
- **Enforced by:** gate check timeouts in `enforcement/gate/gate.py`, runner stage timeouts, `policy/sandbox.md` item 6
- **Status:** enforced (timeouts) / prompt-only (lock ownership convention)

## LSN-007 Stale auto-start application in a persistent volume froze the host on every start
- **ID:** LSN-007
- **Incident:** An old application left in a persistent volume started automatically and froze the host at each start.
- **Rule:** Runtime scripts purge the applications they installed before and after each run.
- **Enforced by:** project runtime scripts (outside this pack); listed in the brief template's STOP section
- **Status:** prompt-only

## LSN-008 Duplicated interface copies drifted
- **ID:** LSN-008
- **Incident:** Consumers kept their own copies of an interface file; the copies diverged.
- **Rule:** The provider owns the interface; consumers reference the provider's directory and never copy.
- **Enforced by:** `enforcement/checks/scope_check.py` (copies of provider files fail when outside allowed globs); review taxonomy category `interface-ownership`
- **Status:** enforced (scope) / prompt-only (detection of copies elsewhere)

## LSN-009 Agent-generated code was over-engineered
- **ID:** LSN-009
- **Incident:** A customer rejected agent-written code as too complex.
- **Rule:** Complexity and SLOC budget in the gate; the task plan sets a size limit per task; prefer extending existing minimal code.
- **Enforced by:** complexity/size checks in `enforcement/lint/preflight_lint.py` and the gate config; size limit in `workflow/templates/task_plan.md`
- **Status:** enforced

## LSN-010 A failing test revealed a real product defect
- **ID:** LSN-010
- **Incident:** An agent weakened a test to go green; the failure was a true product defect.
- **Rule:** Investigate scenario vs defect; never weaken assertions; record a KNOWN_GAP entry and escalate.
- **Enforced by:** `enforcement/checks/test_lock.py`, `policy/failure_handling.md` (spec defect class)
- **Status:** enforced

## LSN-011 Two divergent copies of the lint tool
- **ID:** LSN-011
- **Incident:** Two copies of the lint tool were maintained and diverged.
- **Rule:** One versioned source for each tool; projects consume the pack at a pinned version.
- **Enforced by:** `VERSION`, single `enforcement/lint/preflight_lint.py`, pack-version check in the gate config
- **Status:** enforced

## LSN-012 Rules lived only in prompts and violations repeated
- **ID:** LSN-012
- **Incident:** The same violations recurred because the rules existed only as prompt text.
- **Rule:** Encode rules as hooks, permissions or gate checks; the prompt only points to them.
- **Enforced by:** `enforcement/hooks/pre-commit`, gate checks, permissions
- **Status:** enforced (for rules with a check) / prompt-only (this lessons file marks the rest)

## LSN-013 Removing AI traces from commits destroyed audit provenance
- **ID:** LSN-013
- **Incident:** All AI traces were stripped from commits, and later audits could not tell what was AI-produced.
- **Rule:** Record provenance internally (git notes `refs/notes/ai-provenance` plus evidence bundle); customer disclosure is a separate owner decision.
- **Enforced by:** `policy/provenance.md`, `enforcement/evidence/collect.py`
- **Status:** enforced

## LSN-014 Agents ran with sandbox bypass while reading third-party archives
- **ID:** LSN-014
- **Incident:** Permission checks were disabled while the agent read third-party archives, which is a prompt-injection risk.
- **Rule:** Sandbox by default; repository and archive content is data, not instructions; bypass is a deviation.
- **Enforced by:** `policy/sandbox.md`, runner uses sandboxed engine templates
- **Status:** enforced (runner default) / prompt-only (project-level container setup)

## LSN-015 Documents handed to the owner contained errors
- **ID:** LSN-015
- **Incident:** Documents given to the owner had factual errors.
- **Rule:** Independent review (a different session, with the taxonomy) before a human reads agent-written documents; external claims carry a URL and the mark "external, verify".
- **Enforced by:** `workflow/templates/brief_review.md`; `tests/test_no_project_leaks.py` covers only privacy, not facts
- **Status:** prompt-only

## LSN-016 Local server default context truncated the agent prompt
- **ID:** LSN-016
- **Incident:** A local inference server with a small default context window silently truncated the system prompt and tool schemas, causing the model to print JSON tool calls as plain assistant text instead of emitting structured `tool_use` blocks.
- **Rule:** Configure local servers with context `>= 32,768` tokens (`min_context_tokens = 32768`), qualify every local engine on the `smoke` tier before enabling it, and reject runs with `tool_protocol_errors > 0`.
- **Enforced by:** `policy/local_models.md`, `runner/agent_exec.py` (`tool_protocol_errors` detection), `eval/run_eval.py --record-qualification`
- **Status:** enforced

## LSN-017 Saturated eval tasks hid real capability differences
- **ID:** LSN-017
- **Incident:** v0.1 tasks named the exact bug and file, saturating scores across weaker and stronger models and hiding multi-file, spec-driven, and review capability limits.
- **Rule:** Partition evaluation tasks into calibrated tiers (`smoke` for qualification/regression and `v2` for vague, multi-file, spec-driven, review, and trap tasks) with explicit calibration targets.
- **Enforced by:** `eval/tasks/` (`Tier: smoke|v2`), `eval/run_eval.py --tier`
- **Status:** enforced

## LSN-018 Weak task graders allowed a builder agent to pass with fake checks
- **ID:** LSN-018
- **Incident:** A builder agent made the task-verification gate pass using empty seeded defects, no hidden tests, and a marker file as the pass condition while the director read only task goals; an independent review caught the gap.
- **Rule:** Task verification must enforce structural meta-checks on graders and patches, and the human/director must inspect `check.sh` and `solution.patch` directly with cross-model review.
- **Enforced by:** `eval/tasks/verify_tasks.sh` structural meta-checks (hidden test compile-probe, `src/`/`include/` change checks, `nosrc` failure check, negative review patches, `cheat.patch`) plus independent review
- **Status:** enforced

## LSN-019 Eval graders must not honour self-authored exemptions
- **ID:** LSN-019
- **Incident:** An agent under evaluation could commit its own `Deviation:` trailer to bypass `enforcement/checks/test_lock.py` during grading.
- **Rule:** Evaluation callers and task graders must invoke `test_lock.py` with `--no-deviation` so an agent under test cannot self-exempt protected test files.
- **Enforced by:** `enforcement/checks/test_lock.py` (`--no-deviation`), `eval/run_eval.py`, `eval/tasks/v2_check.sh`, `eval/tasks/18-review-fault-recovery/check.sh`, `eval/orchestrator_goals/01-link-fault-recovery/check.sh`
- **Status:** enforced

## LSN-020 Gate configuration is trusted input; snapshot it from base, never read it from the agent's tree
- **ID:** LSN-020
- **Incident:** `runner/devpack_dispatch.py` passed `workdir/preflight.toml` from the agent-writable working tree to `preflight_lint.py` and `cppcheck_check.py`, allowing a worker to weaken `fail_on`/rules or inject `extra_args` (such as `--addon=` or `--rule-file=`) into host-side gate execution.
- **Rule:** Gate config is trusted input; snapshot it from the base commit (`git show <base>:preflight.toml` into a temporary file outside `workdir`) or from pack/broker-owned project config, never read it from the agent's working tree, validate analyzer options against a strict allowlist, and fail any worker edit to gate/lint config files as a gate-touched scope violation.
- **Enforced by:** `runner/devpack_dispatch.py`, `runner/devpack_run.py`, `runner/devpack_broker.py`, `enforcement/static/cppcheck_check.py`, `enforcement/lint/preflight_lint.py`, `enforcement/checks/scope_check.py`
- **Status:** enforced
