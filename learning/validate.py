#!/usr/bin/env python3
# validate.py - A/B validate a candidate engine profile against the active profile.
#
# Usage:
#   python3 learning/validate.py --engine NAME --candidate FILE
#       [--active FILE] [--tasks all|id,...] [--tier smoke] [--k 3]
#       [--harvest FILE] [--results-json-pair ACTIVE_JSON CANDIDATE_JSON]
#       [--isolation docker|local] [--allow-unsandboxed-local]
#       [--engines-config FILE] [--credentials PATH] [--agent-bin PATH]
#       [--timeout-s 900] [--out-dir learning/reports] [--dry-run] [--help]
#
# Runs an A/B evaluation comparing the active profile against the candidate profile
# on the smoke tier (plus optional tasks from --harvest), or evaluates a pre-recorded
# --results-json-pair offline. Accepts ONLY when lint_findings_first_attempt drops
# strictly and pass rate does not regress. Prints the exact human promotion command
# on ACCEPT and never edits active profiles or runs git commit.
#
# Exit codes:
#   0 - Candidate profile ACCEPTED (or --dry-run validated)
#   1 - Candidate profile REJECTED or runtime error
#   2 - Invalid command-line arguments or candidate profile violates <= 40 lines rule
"""A/B validation gate for candidate engine profiles reusing skilltest aggregation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from eval import run_eval  # noqa: E402
from runner.agent_exec import check_local_isolation_allowed, load_engines, resolve_engine  # noqa: E402
from skilltest.skilltest import aggregate_behaviour  # noqa: E402

MAX_PROFILE_LINES = 40


def _extract_trials_from_payload(payload: Any, engine_name: str = "") -> list[dict[str, Any]]:
    """Normalize trial records from either a flat trial list or an eval results.json object."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("trials"), list):
            trials = [item for item in payload["trials"] if isinstance(item, dict)]
        elif isinstance(payload.get("tasks"), list):
            trials = []
            for task_entry in payload["tasks"]:
                if not isinstance(task_entry, dict):
                    continue
                for run_item in task_entry.get("runs", []):
                    if isinstance(run_item, dict):
                        trials.append(run_item)
        else:
            raise ValueError("Unsupported results JSON structure: missing 'tasks' or 'trials'")
        if engine_name:
            filtered = [t for t in trials if str(t.get("model") or t.get("engine") or "") == engine_name]
            return filtered if filtered else trials
        return trials
    raise ValueError("Results JSON must be a dict or list of trial objects")


def evaluate_ab_results(
    active_trials: Sequence[Mapping[str, Any]],
    candidate_trials: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare active vs candidate trial outcomes and return ACCEPT/REJECT decision."""
    valid_active = [t for t in active_trials if not bool(t.get("harness_error", False))]
    valid_candidate = [t for t in candidate_trials if not bool(t.get("harness_error", False))]

    if not valid_active or not valid_candidate:
        return {
            "decision": "REJECT",
            "reason": "Insufficient valid (non-harness-error) trials for A/B comparison.",
            "active_valid": len(valid_active),
            "candidate_valid": len(valid_candidate),
            "pass_rate_active": None,
            "pass_rate_candidate": None,
            "pass_rate_delta": None,
            "mean_lint_active": None,
            "mean_lint_candidate": None,
            "lint_delta": None,
        }

    active_passes = [bool(t.get("passed", False)) for t in valid_active]
    candidate_passes = [bool(t.get("passed", False)) for t in valid_candidate]
    beh = aggregate_behaviour(candidate_passes, active_passes)
    pass_rate_candidate = round(float(beh["with_skill_rate"]), 4)
    pass_rate_active = round(float(beh["without_skill_rate"]), 4)
    pass_rate_delta = round(float(beh["delta"]), 4)

    active_lint = [
        float(t["lint_findings_first_attempt"])
        for t in valid_active
        if t.get("lint_findings_first_attempt") is not None
    ]
    candidate_lint = [
        float(t["lint_findings_first_attempt"])
        for t in valid_candidate
        if t.get("lint_findings_first_attempt") is not None
    ]

    if not active_lint or not candidate_lint:
        return {
            "decision": "REJECT",
            "reason": "Missing lint_findings_first_attempt telemetry in A/B trials.",
            "active_valid": len(valid_active),
            "candidate_valid": len(valid_candidate),
            "pass_rate_active": pass_rate_active,
            "pass_rate_candidate": pass_rate_candidate,
            "pass_rate_delta": pass_rate_delta,
            "mean_lint_active": None,
            "mean_lint_candidate": None,
            "lint_delta": None,
        }

    mean_lint_active = round(sum(active_lint) / len(active_lint), 4)
    mean_lint_candidate = round(sum(candidate_lint) / len(candidate_lint), 4)
    lint_delta = round(mean_lint_candidate - mean_lint_active, 4)

    if pass_rate_candidate < pass_rate_active:
        decision = "REJECT"
        reason = (
            f"Pass rate regressed from {pass_rate_active:.4f} to {pass_rate_candidate:.4f} "
            f"(delta={pass_rate_delta:+.4f})."
        )
    elif mean_lint_candidate >= mean_lint_active:
        decision = "REJECT"
        reason = (
            f"No gain: lint_findings_first_attempt did not drop "
            f"(active={mean_lint_active:.4f}, candidate={mean_lint_candidate:.4f}, delta={lint_delta:+.4f})."
        )
    else:
        decision = "ACCEPT"
        reason = (
            f"Accepted: lint_findings_first_attempt dropped "
            f"({mean_lint_active:.4f} -> {mean_lint_candidate:.4f}, delta={lint_delta:+.4f}) "
            f"without pass rate regression ({pass_rate_active:.4f} -> {pass_rate_candidate:.4f})."
        )

    return {
        "decision": decision,
        "reason": reason,
        "active_valid": len(valid_active),
        "candidate_valid": len(valid_candidate),
        "pass_rate_active": pass_rate_active,
        "pass_rate_candidate": pass_rate_candidate,
        "pass_rate_delta": pass_rate_delta,
        "mean_lint_active": mean_lint_active,
        "mean_lint_candidate": mean_lint_candidate,
        "lint_delta": lint_delta,
    }


def promotion_commands(engine_name: str, candidate_path: Path, pack_root: Path = PACK_ROOT) -> list[str]:
    """Return exact human promotion shell commands without executing them."""
    target = Path("knowledge") / "engine_profiles" / f"{engine_name}.md"
    try:
        cand_rel = candidate_path.resolve().relative_to(pack_root.resolve())
    except ValueError:
        cand_rel = candidate_path
    return [
        f"cp {cand_rel} {target}",
        f"git add {target}",
        f'git commit -m "chore(learning): promote {engine_name} engine profile"',
    ]


def render_decision_markdown(
    *,
    engine_name: str,
    candidate_path: Path,
    active_path: Path | None,
    result: dict[str, Any],
    promo_cmds: Sequence[str],
) -> str:
    """Format an A/B validation decision report in Markdown."""
    lines = [
        f"# Engine Profile Validation: `{engine_name}`",
        "",
        f"- **Decision:** **{result['decision']}**",
        f"- **Reason:** {result['reason']}",
        f"- **Candidate:** `{candidate_path.name}`",
        f"- **Active:** `{active_path.name if active_path else '(none)'}`",
        "",
        "## A/B Metrics",
        "",
        "| Metric | Active | Candidate | Delta |",
        "|---|---:|---:|---:|",
        f"| Valid trials | {result['active_valid']} | {result['candidate_valid']} | - |",
        f"| Pass rate | {result['pass_rate_active']} | {result['pass_rate_candidate']} | {result['pass_rate_delta']} |",
        f"| Mean lint_findings_first_attempt | {result['mean_lint_active']} | {result['mean_lint_candidate']} | {result['lint_delta']} |",
        "",
    ]
    if result["decision"] == "ACCEPT":
        lines.extend([
            "## Human Promotion Commands",
            "",
            "```bash",
            *promo_cmds,
            "```",
            "",
        ])
    return "\n".join(lines)


def _merge_tasks_with_harvest(
    tasks_dir: Path,
    task_selector: str,
    tier: str,
    harvest_path: Path | None,
    engine_name: str,
) -> list[run_eval.TaskSpec]:
    """Select smoke-tier tasks plus any tasks where ``engine_name`` had harvest findings."""
    all_tasks = run_eval.load_and_validate_tasks(tasks_dir, task_selector)
    if task_selector.strip().lower() != "all":
        return run_eval.filter_tasks_by_tier(all_tasks, tier)

    selected = run_eval.filter_tasks_by_tier(all_tasks, tier)
    selected_ids = {t.task_id for t in selected}
    if harvest_path is not None and harvest_path.is_file():
        try:
            hdata = json.loads(harvest_path.read_text(encoding="utf-8"))
            extra_ids: set[str] = set()
            for item in hdata.get("findings", []):
                if isinstance(item, dict) and item.get("engine") == engine_name:
                    for tid in item.get("task_ids", []):
                        extra_ids.add(str(tid))
            for task in all_tasks:
                if task.task_id in extra_ids and task.task_id not in selected_ids:
                    selected.append(task)
                    selected_ids.add(task.task_id)
        except (OSError, json.JSONDecodeError):
            pass
    return selected


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``learning/validate.py``."""
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="A/B validate a candidate engine profile against the active profile.",
    )
    parser.add_argument("--engine", required=True, help="Engine name (e.g. local, haiku)")
    parser.add_argument("--candidate", required=True, help="Path to candidate profile markdown")
    parser.add_argument(
        "--active",
        default="",
        help="Optional active profile path (default: knowledge/engine_profiles/<engine>.md)",
    )
    parser.add_argument("--tasks", default="all", help="Task selection: 'all' or comma-separated IDs")
    parser.add_argument("--tier", choices=("smoke", "v2", "all"), default="smoke", help="Task tier (default: smoke)")
    parser.add_argument("--k", type=int, default=3, help="Trials per task per arm (default: 3)")
    parser.add_argument("--harvest", default="", help="Optional harvest JSON to also include finding tasks")
    parser.add_argument(
        "--results-json-pair",
        nargs=2,
        metavar=("ACTIVE_JSON", "CANDIDATE_JSON"),
        default=None,
        help="Evaluate pre-recorded A/B results JSON files offline without running agents",
    )
    parser.add_argument("--isolation", choices=("docker", "local"), default="docker", help="Isolation mode (default: docker)")
    parser.add_argument("--allow-unsandboxed-local", action="store_true", help="Authorize --isolation local")
    parser.add_argument(
        "--engines-config",
        default=str(PACK_ROOT / "runner" / "engines.example.toml"),
        help="Path to engines.toml",
    )
    parser.add_argument("--credentials", default="", help="Optional Claude credentials file path")
    parser.add_argument("--agent-bin", default="claude", help="Agent CLI executable (default: claude)")
    parser.add_argument("--timeout-s", type=int, default=900, help="Per-trial timeout seconds (default: 900)")
    parser.add_argument(
        "--fixture-dir",
        default=str(PACK_ROOT / "eval" / "fixture"),
        help="Path to C++ evaluation fixture directory",
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(PACK_ROOT / "eval" / "tasks"),
        help="Path to evaluation tasks directory",
    )
    parser.add_argument(
        "--out-dir",
        default=str(PACK_ROOT / "learning" / "reports"),
        help="Directory for decision reports (default: learning/reports)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print A/B validation plan without running agents")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``learning/validate.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.k < 1 or args.timeout_s < 1:
        print("error: --k and --timeout-s must be >= 1", file=sys.stderr)
        return 2

    candidate_path = Path(args.candidate).resolve()
    if not candidate_path.is_file():
        print(f"error: candidate profile not found: {candidate_path}", file=sys.stderr)
        return 2

    candidate_lines = candidate_path.read_text(encoding="utf-8").splitlines()
    if len(candidate_lines) > MAX_PROFILE_LINES:
        print(
            f"error: candidate profile exceeds {MAX_PROFILE_LINES} lines ({len(candidate_lines)} lines)",
            file=sys.stderr,
        )
        return 2

    default_active = PACK_ROOT / "knowledge" / "engine_profiles" / f"{args.engine}.md"
    active_path: Path | None = (
        Path(args.active).resolve()
        if args.active
        else (default_active if default_active.is_file() else None)
    )
    if args.active and (active_path is None or not active_path.is_file()):
        print(f"error: active profile not found: {args.active}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir).resolve()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.results_json_pair is not None:
        active_json_path = Path(args.results_json_pair[0]).resolve()
        cand_json_path = Path(args.results_json_pair[1]).resolve()
        if not active_json_path.is_file() or not cand_json_path.is_file():
            print("error: --results-json-pair files must exist", file=sys.stderr)
            return 1
        try:
            active_payload = json.loads(active_json_path.read_text(encoding="utf-8"))
            cand_payload = json.loads(cand_json_path.read_text(encoding="utf-8"))
            active_trials = _extract_trials_from_payload(active_payload, args.engine)
            cand_trials = _extract_trials_from_payload(cand_payload, args.engine)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        decision = evaluate_ab_results(active_trials, cand_trials)
        cmds = promotion_commands(args.engine, candidate_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_md = out_dir / f"validate-{args.engine}-{date_str}.md"
        report_json = out_dir / f"validate-{args.engine}-{date_str}.json"
        report_json.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        report_md.write_text(
            render_decision_markdown(
                engine_name=args.engine,
                candidate_path=candidate_path,
                active_path=active_path,
                result=decision,
                promo_cmds=cmds,
            ),
            encoding="utf-8",
        )
        print(f"{decision['decision']}: {decision['reason']}")
        print(f"Report: {report_md}")
        if decision["decision"] == "ACCEPT":
            print("To promote (human action required):")
            for cmd in cmds:
                print(f"  {cmd}")
            return 0
        return 1

    fixture_dir = Path(args.fixture_dir).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    harvest_path = Path(args.harvest).resolve() if args.harvest else None
    try:
        run_eval.validate_fixture_dir(fixture_dir)
        tasks = _merge_tasks_with_harvest(tasks_dir, args.tasks, args.tier, harvest_path, args.engine)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"DRY-RUN A/B validation plan for engine '{args.engine}':")
        print(f"  active: {active_path or '(none)'}")
        print(f"  candidate: {candidate_path} ({len(candidate_lines)} lines)")
        print(f"  tasks ({len(tasks)}): {', '.join(t.task_id for t in tasks)}")
        print(f"  k: {args.k} ({len(tasks) * args.k * 2} total trials)")
        return 0

    iso_ok, iso_msg = check_local_isolation_allowed(
        args.isolation,
        allow_unsandboxed_local=args.allow_unsandboxed_local,
        workdir=PACK_ROOT,
    )
    if not iso_ok:
        print(f"error: {iso_msg}", file=sys.stderr)
        return 1

    cfg_path = Path(args.engines_config).resolve()
    loaded_engines = load_engines(cfg_path) if cfg_path.is_file() else None
    engine_cfg = resolve_engine(args.engine, loaded_engines)
    cred_path = Path(args.credentials).resolve() if args.credentials else None
    if engine_cfg.is_cloud and cred_path is None:
        print("error: credentials required for cloud engine validation", file=sys.stderr)
        return 1

    docker_bin: str | None = None
    if args.isolation == "docker":
        try:
            docker_bin = run_eval.ensure_docker_image(PACK_ROOT / "eval" / "Dockerfile")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="devpack-validate-") as tmp:
        tmp_root = Path(tmp)
        arm_trials: dict[str, list[dict[str, Any]]] = {"active": [], "candidate": []}
        for arm_name, prof_path in (("active", active_path), ("candidate", candidate_path)):
            arm_root = tmp_root / arm_name
            runs_root = out_dir / "validate_runs" / f"{date_str}-{args.engine}-{arm_name}"
            for task in tasks:
                for run_idx in range(1, args.k + 1):
                    tr = run_eval.run_single_trial(
                        task=task,
                        model=args.engine,
                        run_index=run_idx,
                        run_root=arm_root,
                        fixture_dir=fixture_dir,
                        credentials_path=cred_path,
                        isolation=args.isolation,
                        agent_bin=args.agent_bin,
                        timeout_s=args.timeout_s,
                        docker_bin=docker_bin,
                        runs_root=runs_root,
                        engine_config=engine_cfg,
                        allow_unsandboxed_local=args.allow_unsandboxed_local,
                        profile_overrides={args.engine: prof_path},
                    )
                    arm_trials[arm_name].append({
                        "task_id": tr.task_id,
                        "passed": tr.passed,
                        "harness_error": tr.harness_error,
                        "lint_findings_first_attempt": tr.lint_findings_first_attempt,
                    })

    decision = evaluate_ab_results(arm_trials["active"], arm_trials["candidate"])
    cmds = promotion_commands(args.engine, candidate_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_md = out_dir / f"validate-{args.engine}-{date_str}.md"
    report_json = out_dir / f"validate-{args.engine}-{date_str}.json"
    report_json.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(
        render_decision_markdown(
            engine_name=args.engine,
            candidate_path=candidate_path,
            active_path=active_path,
            result=decision,
            promo_cmds=cmds,
        ),
        encoding="utf-8",
    )
    print(f"{decision['decision']}: {decision['reason']}")
    print(f"Report: {report_md}")
    if decision["decision"] == "ACCEPT":
        print("To promote (human action required):")
        for cmd in cmds:
            print(f"  {cmd}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
