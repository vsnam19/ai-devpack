#!/usr/bin/env python3
# skilltest.py - Lint skills and run controlled, recorded skill experiments.
#
# Usage: python3 skilltest/skilltest.py {lint,run,report} ...
# Exit codes: 0 success; 1 lint or trial failure; 2 invalid arguments.
"""Deterministic skill linting and offline-capable A/B experiment harness."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from runner.agent_exec import AgentRequest, EngineConfig, run_agent


MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_LINES = 200


def load_config(skill_dir: Path) -> dict[str, Any]:
    """Load a skill test configuration, returning an empty mapping when unavailable."""
    config = skill_dir / "tests" / "skilltest.toml"
    if not config.is_file() or tomllib is None:
        return {}
    with config.open("rb") as handle:
        return tomllib.load(handle)


def parse_frontmatter(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Parse compact YAML-like frontmatter and return metadata, body, and errors."""
    errors: list[str] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines, ["missing frontmatter"]
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return {}, lines, ["unterminated frontmatter"]
    meta: dict[str, str] = {}
    for line in lines[1:closing]:
        if ":" not in line:
            errors.append("invalid frontmatter entry")
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, lines[closing + 1 :], errors


def lint_skill(skill_dir: Path) -> list[str]:
    """Return every deterministic conformance error for one skill directory."""
    errors: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return ["missing SKILL.md"]
    metadata, body, errors = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = metadata.get("name", "")
    if not name:
        errors.append("frontmatter missing name")
    elif name != skill_dir.name:
        errors.append("frontmatter name does not match directory")
    description = metadata.get("description", "")
    if not description:
        errors.append("frontmatter missing description")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append("description exceeds 1024 characters")
    if len(body) > MAX_BODY_LINES:
        errors.append("body exceeds 200 lines")
    content = skill_file.read_text(encoding="utf-8")
    if "/" + "home/" in content or "\\" in content and ":\\" in content:
        errors.append("absolute path found")
    for line in content.splitlines():
        if "](" in line:
            target = line.split("](", 1)[1].split(")", 1)[0].split("#", 1)[0]
            if target and not target.startswith(("http://", "https://", "mailto:")):
                if not (skill_dir / target).resolve().is_file():
                    errors.append(f"relative link does not resolve: {target}")
    config_path = skill_dir / "tests" / "skilltest.toml"
    config = load_config(skill_dir)
    if not config_path.is_file() or not config:
        errors.append("missing tests/skilltest.toml")
        return errors
    triggers = config.get("trigger", [])
    positives = sum(1 for item in triggers if item.get("expect") is True)
    negatives = sum(1 for item in triggers if item.get("expect") is False)
    if positives < 2:
        errors.append("requires at least two trigger-positive cases")
    if negatives < 2:
        errors.append("requires at least two trigger-negative cases")
    if not config.get("behaviour", []):
        errors.append("requires at least one behaviour case")
    return errors


def transcript_triggered(transcript: str, skill_name: str) -> bool:
    """Identify a Skill selection or a read of this skill's installed markdown file."""
    suffix = f".claude/skills/{skill_name}/SKILL.md"
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message", event)
        for block in message.get("content", []) if isinstance(message, dict) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = block.get("name")
            inputs = block.get("input", {})
            encoded = json.dumps(inputs)
            if tool == "Skill" and skill_name in encoded:
                return True
            if tool == "Read" and suffix in encoded:
                return True
    return False


def aggregate_behaviour(with_skill: list[bool], without_skill: list[bool]) -> dict[str, Any]:
    """Calculate A/B rates, delta, and the required no-benefit verdict."""
    with_rate = sum(with_skill) / len(with_skill) if with_skill else 0.0
    without_rate = sum(without_skill) / len(without_skill) if without_skill else 0.0
    delta = round(with_rate - without_rate, 6)
    return {"with_skill_rate": with_rate, "without_skill_rate": without_rate,
            "delta": delta, "no_measurable_benefit": delta <= 0.0}


def _copy_skill(skill_dir: Path, workdir: Path, name: str) -> None:
    """Install one skill at the project location used by Claude Code."""
    target = workdir / ".claude" / "skills" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_dir, target)


def _trial_run_dir(root: Path, label: str) -> Path:
    """Create an isolated run directory with a stable, readable trial label."""
    path = root / "runs" / label
    path.mkdir(parents=True, exist_ok=True)
    return path


def _execute_trial(skill_dir: Path, engine: str, prompt: str, enabled: bool, run_dir: Path,
                   fixture: Path | None, agent_bin: str, isolation: str,
                   check_command: str | None = None) -> tuple[bool, bool, dict[str, Any]]:
    """Run one agent trial and return trigger status plus serialized telemetry."""
    with tempfile.TemporaryDirectory(prefix="skilltest-") as temp:
        workdir = Path(temp) / "work"
        workdir.mkdir()
        if fixture is not None:
            shutil.copytree(fixture, workdir, dirs_exist_ok=True)
        if enabled:
            _copy_skill(skill_dir, workdir, skill_dir.name)
        result = run_agent(AgentRequest(
            engine=EngineConfig(name=engine, model=engine), prompt=prompt, workdir=workdir,
            run_dir=run_dir, step_name="skilltest", role="skilltest", skills_enabled=enabled,
            agent_bin=agent_bin, isolation=isolation,
        ))
        raw = result.transcript_path.read_text(encoding="utf-8") if result.transcript_path.is_file() else ""
        passed = False
        if check_command is not None:
            check_env = {**os.environ, "DEVPACK_ROOT": str(REPOSITORY_ROOT)}
            passed = subprocess.run(check_command, shell=True, cwd=workdir, env=check_env, check=False).returncode == 0
        return transcript_triggered(raw, skill_dir.name), passed, asdict(result)


def run_experiment(args: argparse.Namespace) -> int:
    """Run or plan trigger and behaviour trials for a skill."""
    skill_dir = Path(args.skill_dir).resolve()
    errors = lint_skill(skill_dir)
    if errors:
        for error in errors:
            print(f"lint: {error}", file=sys.stderr)
        return 1
    config = load_config(skill_dir)
    engines = [value.strip() for value in args.engines.split(",") if value.strip()]
    if not engines or args.k < 1:
        print("engines and k must be non-empty", file=sys.stderr)
        return 2
    plan = [(engine, kind, index + 1, case.get("name", "unnamed"))
            for engine in engines for kind, cases in (("trigger", config["trigger"]), ("with_skill", config["behaviour"]), ("without_skill", config["behaviour"]))
            for case in cases for index in range(args.k)]
    if args.dry_run:
        print("TRIAL PLAN")
        for engine, arm, count, name in plan:
            print(f"{engine} {arm} {count}/{args.k} {name}")
        return 0
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    output = Path(args.results_root) / date / skill_dir.name
    output.mkdir(parents=True, exist_ok=True)
    trials: list[dict[str, Any]] = []
    serial = 0
    for engine in engines:
        for case in config["trigger"]:
            for _ in range(args.k):
                serial += 1
                triggered, _, telemetry = _execute_trial(skill_dir, engine, case["prompt"], True,
                    _trial_run_dir(output, f"{serial:03d}-trigger-{engine}"), None, args.agent_bin, args.isolation)
                trials.append({"engine": engine, "arm": "trigger", "case": case["name"], "expected": case["expect"], "triggered": triggered, "telemetry": telemetry})
        for arm, enabled in (("with_skill", True), ("without_skill", False)):
            for case in config["behaviour"]:
                fixture = skill_dir / "tests" / case["fixture"]
                for _ in range(args.k):
                    serial += 1
                    _, passed, telemetry = _execute_trial(skill_dir, engine, case["prompt"], enabled,
                        _trial_run_dir(output, f"{serial:03d}-{arm}-{engine}"), fixture, args.agent_bin, args.isolation, case["check"])
                    trials.append({"engine": engine, "arm": arm, "case": case["name"], "passed": passed, "telemetry": telemetry})
    report = {"skill": skill_dir.name, "trials": trials}
    trigger_report: dict[str, Any] = {}
    for engine in engines:
        samples = [trial for trial in trials if trial["engine"] == engine and trial["arm"] == "trigger"]
        true_positive = sum(1 for trial in samples if trial["expected"] and trial["triggered"])
        false_positive = sum(1 for trial in samples if not trial["expected"] and trial["triggered"])
        false_negative = sum(1 for trial in samples if trial["expected"] and not trial["triggered"])
        case_rates = {name: sum(item["triggered"] for item in samples if item["case"] == name) /
                      sum(1 for item in samples if item["case"] == name)
                      for name in sorted({item["case"] for item in samples})}
        trigger_report[engine] = {
            "precision": true_positive / (true_positive + false_positive)
            if true_positive + false_positive else 0.0,
            "recall": true_positive / (true_positive + false_negative)
            if true_positive + false_negative else 0.0,
            "per_case_rate": case_rates,
        }
    report["trigger"] = trigger_report
    report["behaviour"] = {engine: aggregate_behaviour(
        [trial["passed"] for trial in trials if trial["engine"] == engine and trial["arm"] == "with_skill"],
        [trial["passed"] for trial in trials if trial["engine"] == engine and trial["arm"] == "without_skill"],
    ) for engine in engines}
    (output / "summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    lines = [f"# Skilltest: {skill_dir.name}", "", f"Trials: {len(trials)}", "", "## Trigger"]
    lines += [f"- {engine}: precision={item['precision']:.2f}, recall={item['recall']:.2f}, per-case={item['per_case_rate']}" for engine, item in report["trigger"].items()]
    lines += ["", "## Behaviour"]
    lines += [f"- {engine}: with={item['with_skill_rate']:.2f}, without={item['without_skill_rate']:.2f}, delta={item['delta']:.2f}; no measurable benefit={item['no_measurable_benefit']}" for engine, item in report["behaviour"].items()]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output / "summary.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse command line arguments and dispatch the requested operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    lint = sub.add_parser("lint", help="validate one skill")
    lint.add_argument("skill_dir")
    run = sub.add_parser("run", help="run or plan an A/B experiment")
    run.add_argument("skill_dir")
    run.add_argument("--engines", required=True)
    run.add_argument("--k", type=int, default=3)
    run.add_argument("--isolation", choices=("local", "docker"), default="local")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--agent-bin", default="claude")
    run.add_argument("--results-root", default="skilltest/results")
    report = sub.add_parser("report", help="print an existing JSON summary")
    report.add_argument("summary_json")
    args = parser.parse_args(argv)
    if args.command == "lint":
        errors = lint_skill(Path(args.skill_dir))
        if errors:
            print("\n".join(f"lint: {error}" for error in errors), file=sys.stderr)
            return 1
        print("OK")
        return 0
    if args.command == "run":
        return run_experiment(args)
    print(Path(args.summary_json).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
