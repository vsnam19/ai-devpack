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
import re
import shlex
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

from runner.agent_exec import AgentRequest, load_engines, run_agent, update_run_json
from runner.devpack_logs import Redactor, load_run_dir, write_rendered_outputs


MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_LINES = 200
ABSOLUTE_PATH_PATTERN = re.compile(r"(?:^|[\s`(])(?:/(?:Users|home|opt|usr|var|etc|tmp|mnt)/|[A-Za-z]:[\\\\/])")
MARKDOWN_LINK_PATTERN = re.compile(r"\]\(([^)]+)\)")


def _minimal_toml_loads(text: str) -> dict[str, Any]:
    """Parse flat TOML scalars and arrays of tables used by skilltest on Python 3.10.

    Support quoted strings, booleans, and integers; reject unsupported syntax.
    """
    root: dict[str, Any] = {}
    current = root
    for line_number, line in enumerate(text.splitlines(), 1):
        quote = None
        escaped = False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            elif quote is None and char in ("'", '"'):
                quote = char
            elif quote is None and char == '#':
                line = line[:index]
                break
        line = line.strip()
        if not line:
            continue
        table = re.fullmatch(r"\[\[([A-Za-z_][\w-]*)\]\]", line)
        if table:
            current = {}
            root.setdefault(table[1], []).append(current)
            continue
        entry = re.fullmatch(r"([A-Za-z_][\w-]*)\s*=\s*(.+)", line)
        if not entry:
            raise ValueError(f"unsupported TOML at line {line_number}")
        key, value = entry.groups()
        if key in current:
            raise ValueError(f"duplicate TOML key at line {line_number}")
        if value in ('true', 'false'):
            parsed = value == 'true'
        elif re.fullmatch(r'[+-]?[0-9]+', value):
            parsed = int(value)
        elif value.startswith('"') and value.endswith('"'):
            parsed = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            parsed = value[1:-1]
        else:
            raise ValueError(f"unsupported TOML value at line {line_number}")
        current[key] = parsed
    return root


def load_config(skill_dir: Path) -> dict[str, Any]:
    """Load a skill test configuration with a Python 3.10 stdlib fallback."""
    config = skill_dir / "tests" / "skilltest.toml"
    if not config.is_file():
        return {}
    text = config.read_text(encoding="utf-8")
    return tomllib.loads(text) if tomllib is not None else _minimal_toml_loads(text)


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
    if ABSOLUTE_PATH_PATTERN.search(content):
        errors.append("absolute path found")
    for line in content.splitlines():
        for match in MARKDOWN_LINK_PATTERN.finditer(line):
            target = match.group(1).split("#", 1)[0]
            if target.startswith("/"):
                errors.append("absolute path found")
                continue
            if target and not target.startswith(("http://", "https://", "mailto:")):
                if not (skill_dir / target).resolve().is_file():
                    errors.append(f"relative link does not resolve: {target}")
    config_path = skill_dir / "tests" / "skilltest.toml"
    try:
        config = load_config(skill_dir)
    except ValueError as error:
        return [*errors, f"invalid tests/skilltest.toml: {error}"]
    if not config_path.is_file() or not config:
        errors.append("missing tests/skilltest.toml")
        return errors
    for kind in ("trigger", "behaviour"):
        cases = config.get(kind, [])
        if not isinstance(cases, list) or any(not isinstance(item, dict) for item in cases):
            errors.append(f"{kind} must be an array of tables")
            return errors
        for index, item in enumerate(cases, 1):
            for field in ("name", "prompt"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(f"{kind} {index}: {field} must be a non-empty string")
            if kind == "trigger" and not isinstance(item.get("expect"), bool):
                errors.append(f"trigger {index}: expect must be a boolean")
    triggers = config.get("trigger", [])
    positives = sum(1 for item in triggers if item.get("expect") is True)
    negatives = sum(1 for item in triggers if item.get("expect") is False)
    if positives < 2:
        errors.append("requires at least two trigger-positive cases")
    if negatives < 2:
        errors.append("requires at least two trigger-negative cases")
    if not config.get("behaviour", []):
        errors.append("requires at least one behaviour case")
    fixtures_root = skill_dir / "tests" / "fixtures"
    input_dirs = list(fixtures_root.glob("*/input")) if fixtures_root.is_dir() else []
    declared_inputs = {item["fixture"] for item in config.get("behaviour", [])
                       if isinstance(item.get("fixture"), str)}
    for item in config.get("behaviour", []):
        fixture = item.get("fixture")
        grader = item.get("grader")
        if not isinstance(fixture, str) or not (skill_dir / "tests" / fixture).is_dir():
            errors.append(f"behaviour fixture does not resolve: {fixture}")
        if not isinstance(grader, str) or not (skill_dir / "tests" / grader / "check.sh").is_file():
            errors.append(f"behaviour grader does not resolve: {grader}")
        if not isinstance(item.get("check"), str) or not item["check"].strip():
            errors.append("behaviour check must be a non-empty command")
    for input_dir in input_dirs:
        relative = input_dir.relative_to(skill_dir / "tests").as_posix()
        if relative not in declared_inputs:
            errors.append(f"unreferenced fixture directory: {relative}")
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
    shutil.copytree(skill_dir, target, ignore=shutil.ignore_patterns("tests", "__pycache__"))


def _trial_run_dir(root: Path, label: str) -> Path:
    """Create an isolated run directory with a stable, readable trial label."""
    path = root / "runs" / label
    path.mkdir(parents=True, exist_ok=True)
    return path


def _execute_trial(skill_dir: Path, engine: str, prompt: str, enabled: bool, run_dir: Path,
                   fixture: Path | None, agent_bin: str, isolation: str,
                   check_command: str | None = None, grader: Path | None = None,
                   expectations: dict[str, Any] | None = None,
                   engines_config: Path | str = REPOSITORY_ROOT / "runner/engines.example.toml",
                   expected_trigger: bool | None = None) -> tuple[bool, bool, dict[str, Any]]:
    """Run one agent trial and return trigger status plus serialized telemetry."""
    run_dir.mkdir(parents=True, exist_ok=True)
    update_run_json(run_dir, kind="skilltest", engines=[engine], task=prompt, outcome="running")
    redactor = Redactor()
    triggered = passed = False
    telemetry: dict[str, Any] = {}
    outcome = "error"
    reason = "Trial did not complete"
    grader_output = ""
    try:
        with tempfile.TemporaryDirectory(prefix="skilltest-") as temp:
            workdir = Path(temp) / "work"
            workdir.mkdir()
            if fixture is not None:
                shutil.copytree(fixture, workdir, dirs_exist_ok=True)
            if enabled:
                _copy_skill(skill_dir, workdir, skill_dir.name)
            result = run_agent(AgentRequest(
                engine=load_engines(engines_config).get_engine(engine), prompt=prompt, workdir=workdir,
                run_dir=run_dir, step_name="skilltest", role="skilltest", skills_enabled=enabled,
                agent_bin=agent_bin, isolation=isolation,
            ))
            telemetry = asdict(result)
            raw = result.transcript_path.read_text(encoding="utf-8") if result.transcript_path.is_file() else ""
            triggered = transcript_triggered(raw, skill_dir.name)
            if result.status != "ok" or result.tool_protocol_errors:
                reason = f"Agent error: {result.error or result.status}; tool protocol errors={result.tool_protocol_errors}"
            elif check_command is None:
                passed = triggered == expected_trigger if expected_trigger is not None else True
                outcome = "pass" if passed else "fail"
                reason = "" if passed else f"FAIL: expected triggered={expected_trigger}, observed {triggered}"
            else:
                grader_names = {path.name for path in grader.rglob("*") if path.is_file()} if grader else set()
                contaminated = any(path.name in grader_names for path in workdir.rglob("*") if path.is_file())
                if contaminated:
                    outcome = "fail"
                    reason = "FAIL: grader material found in candidate workspace"
                elif grader is None:
                    reason = "Grader directory is missing"
                else:
                    check_env = {**os.environ, "DEVPACK_ROOT": str(REPOSITORY_ROOT)}
                    for key, value in (expectations or {}).items():
                        check_env[f"SKILLTEST_EXPECTED_{key.upper()}"] = str(value)
                    check = subprocess.run(
                        [*shlex.split(check_command), str(workdir)], cwd=grader, env=check_env,
                        check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                    grader_output = check.stdout
                    passed = check.returncode == 0
                    outcome = "pass" if passed else "fail"
                    lines = [line.strip() for line in grader_output.splitlines() if line.strip()]
                    failures = [line for line in lines if re.search(r"FAIL|ERROR|error:|failed", line)]
                    reason = "" if passed else next(iter(failures or lines), f"Grader exited with code {check.returncode}")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = f"Trial error: {error}"
    finally:
        reason = redactor.redact(reason)
        if check_command is not None:
            steps = run_dir / "steps"
            steps.mkdir(exist_ok=True)
            (steps / "02-grader.txt").write_text(
                redactor.redact(grader_output) + f"\n{outcome.upper()}: {reason or 'Grader passed'}\n", encoding="utf-8")
        update_run_json(run_dir, finished=datetime.now(timezone.utc).isoformat(), outcome=outcome)
        write_rendered_outputs(load_run_dir(run_dir, redactor), run_dir)
    telemetry.update(reason=reason, outcome=outcome)
    return triggered, passed, telemetry


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
                    _trial_run_dir(output, f"{serial:03d}-trigger-{engine}"), None, args.agent_bin, args.isolation, engines_config=args.engines_config, expected_trigger=case["expect"])
                trials.append({"engine": engine, "arm": "trigger", "case": case["name"], "expected": case["expect"], "triggered": triggered, "reason": telemetry["reason"], "telemetry": telemetry})
        for arm, enabled in (("with_skill", True), ("without_skill", False)):
            for case in config["behaviour"]:
                fixture = skill_dir / "tests" / case["fixture"]
                grader = skill_dir / "tests" / case["grader"]
                for _ in range(args.k):
                    serial += 1
                    _, passed, telemetry = _execute_trial(skill_dir, engine, case["prompt"], enabled,
                        _trial_run_dir(output, f"{serial:03d}-{arm}-{engine}"), fixture, args.agent_bin, args.isolation,
                        case["check"], grader, {key: case[key] for key in ("category", "severity", "verdict") if key in case},
                        engines_config=args.engines_config)
                    trials.append({"engine": engine, "arm": arm, "case": case["name"], "passed": passed, "reason": telemetry["reason"], "telemetry": telemetry})
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
    lines += ["", "## Trials"]
    lines += [f"- {trial['engine']} {trial['arm']} {trial['case']}: {trial['telemetry']['outcome']}; {trial['reason'] or 'Passed'}" for trial in trials]
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
    run.add_argument("--engines-config", default=str(REPOSITORY_ROOT / "runner/engines.example.toml"))
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
