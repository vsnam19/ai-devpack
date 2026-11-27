#!/usr/bin/env python3
"""Inspect Pack runs and checkpoint evidence from a read-only terminal dashboard."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

if __name__ == "__main__":
    sys.dont_write_bytecode = True
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner.checkpoints import default_state_dir, load_pipeline_config, resolve_state_dir  # noqa: E402
from runner.workspace_scope import parse_toml_with_arrays  # noqa: E402
from runner.dash.files import Files, JSON_LIMIT, identifier, mapping, resolve_root_parent  # noqa: E402
from runner.dash.reader import FILTER_HELP, Settings, Reader, TERMINAL, terms  # noqa: E402
from runner.dash.ui import event_line, interface, plain  # noqa: E402

INTERVALS = {"stream_poll_s", "active_poll_s", "discovery_poll_s", "quiet_threshold_s", "history_limit"}
ROOTS = {"log_roots", "evidence_roots"}


def parser(*, description=__doc__):
    result = argparse.ArgumentParser(description=description, epilog=FILTER_HELP)
    result.add_argument("--workdir", default=".")
    result.add_argument("--dashboard-config")
    result.add_argument("--project-config")
    result.add_argument("--runs-root")
    result.add_argument("--plans-root")
    result.add_argument("--state-dir")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--plain", action="store_true")
    mode.add_argument("--json", action="store_true")
    result.add_argument("--once", action="store_true", help="Read one snapshot or initial tail page")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("jobs").add_argument("--filter", default="", help=FILTER_HELP)
    for name in ("show", "gate", "tail"):
        command = commands.add_parser(name)
        command.add_argument("job")
        if name == "tail":
            command.add_argument("--kind", choices=("all", "tool", "message"), default="all")
    commands.add_parser("plans")
    commands.add_parser("plan").add_argument("plan_id")
    approvals = commands.add_parser("approvals")
    approvals.add_argument("--status", choices=("pending", "decided", "all"), default="all")
    approvals.add_argument("--plain", action="store_true", default=argparse.SUPPRESS)
    return result


def config(args, *, active_poll_default=None):
    files = Files()
    workdir = Path(args.workdir).resolve(strict=True)
    if not workdir.is_dir():
        raise ValueError("workdir must be a directory")
    project_config = Path(args.project_config).resolve(strict=True) if args.project_config else None
    project_file = project_config or workdir / ".devpack" / "project.toml"

    def read_config(path, *, optional=False):
        try:
            return files.read(path, JSON_LIMIT)
        except FileNotFoundError:
            if optional:
                return b""
            raise

    def relative(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected a nonempty root path")
        path = Path(value)
        if ".." in path.parts:
            raise ValueError("root paths must not contain parent traversal")
        return workdir / path

    raw = read_config(project_file, optional=project_config is None)
    project = parse_toml_with_arrays(raw.decode("utf-8"))

    def state_root(value):
        candidate = workdir / value
        if Path(os.path.abspath(candidate)).is_relative_to(workdir):
            raise ValueError("state directory must be outside the workdir")
        return resolve_root_parent(candidate)

    if args.state_dir:
        state_path = args.state_dir.strip() or default_state_dir(workdir)
        state = resolve_state_dir(state_root(state_path), project_root=workdir, agent_worktree=workdir, explicit=True)
    else:
        configured = mapping(project.get("pipeline")).get("state_dir")
        configured = configured.strip() if isinstance(configured, str) else ""
        state_path = os.environ.get("DEVPACK_STATE_DIR", "").strip() or configured or default_state_dir(workdir)
        state = Path(load_pipeline_config(workdir, project_config_path=project_config, trusted_project_bytes=raw,
                                         override_state_dir=state_root(state_path), agent_worktree=workdir).state_dir)
    options = {}
    if args.dashboard_config:
        dashboard_path = Path(os.path.abspath(args.dashboard_config))
        if dashboard_path.is_relative_to(workdir):
            raise ValueError("dashboard config must be outside the worker workdir")
        document = parse_toml_with_arrays(read_config(dashboard_path).decode("utf-8"))
        options = document.get("dashboard", {})
        if not isinstance(options, dict) or options.keys() - (INTERVALS | ROOTS | {"runs_root", "plans_root", "engines_config"}):
            raise ValueError("invalid dashboard settings")
    for key in INTERVALS & options.keys():
        if type(options[key]) is not int or options[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ROOTS & options.keys():
        if not isinstance(options[key], list):
            raise ValueError(f"{key} must be an array of paths")
        options[key] = tuple(relative(value) for value in options[key])
    configured_runs = options.pop("runs_root", "runs")
    runs = relative(args.runs_root if args.runs_root is not None else configured_runs)
    configured_plans = options.pop("plans_root", project.get("plans_root", "plans"))
    plans = relative(args.plans_root if args.plans_root is not None else configured_plans)
    if args.dashboard_config and any(
        dashboard_path.is_relative_to(root)
        for root in (runs, plans, *options.get("log_roots", ()), *options.get("evidence_roots", ()))
    ):
        raise ValueError("dashboard config must be outside artifact roots")
    engine_file = options.pop("engines_config", None)
    project_engine_file = project.get("engines_config")
    if engine_file is not None:
        engine_bytes = read_config(relative(engine_file))
    elif project_engine_file:
        if not isinstance(project_engine_file, str):
            raise ValueError("expected an engines config path")
        candidate = Path(project_engine_file.strip())
        if ".." in candidate.parts:
            raise ValueError("engines config must not contain parent traversal")
        try:
            engine_bytes = read_config(project_file.parent / candidate)
        except FileNotFoundError:
            engine_bytes = read_config(Path(__file__).resolve().parents[1] / candidate, optional=True)
    else:
        engine_bytes = read_config(workdir / ".devpack" / "engines.toml", optional=True)
        if not engine_bytes:
            engine_bytes = read_config(Path(__file__).resolve().with_name("engines.example.toml"), optional=True)
    engines = parse_toml_with_arrays(engine_bytes.decode("utf-8"))
    models = tuple((key, spec["model"]) for key, spec in mapping(engines.get("engines")).items()
                   if isinstance(spec, dict) and isinstance(spec.get("model"), str))
    if active_poll_default is not None:
        options.setdefault("active_poll_s", active_poll_default)
    return Settings(workdir, runs, state, project_config, plans=plans, models=models, **options)


def dumps(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def available(snapshot, command):
    sources = snapshot["sources"]
    if command == "plans":
        return sources["plans"] in ("current", "partial", "missing")
    if command == "plan":
        return bool(snapshot["plans"])
    if command == "approvals":
        return sources["approvals"] in ("current", "stale", "partial")
    if command in ("show", "gate", "tail"):
        if snapshot["ambiguous"] or not snapshot["jobs"]:
            return False
        if command == "tail":
            return sources["stream"] in ("empty", "plain", "jsonl", "uncertain")
        return not snapshot["jobs"][0]["read_failed"]
    return sources["runs"] == "current" or sources["queue"] == "current"


def tail(reader, args):
    options = {"job_id": args.job, "event_kind": args.kind, "include_gates": False}
    snapshot = reader.snapshot(**options)
    if not available(snapshot, "tail"):
        notice = {"kind": "notice", "text": "Job or stream unavailable", "sources": snapshot["sources"]}
        print(dumps(notice) if args.json else notice["text"])
        return 1
    previous_health = None
    last = 0
    stream = reader.stream
    while True:
        health = snapshot["sources"]["stream"]
        if health != previous_health and health not in ("jsonl", "plain"):
            job = snapshot["jobs"][0] if snapshot["jobs"] else {}
            notice = {
                "schema_version": 1, "kind": "notice",
                "text": next((item for item in job.get("notices", []) if item.startswith("No stream")), f"Stream {health or 'unavailable'}"),
                "observed_at": snapshot["observed_at"], "mtime": job.get("stream_mtime"), "age_s": job.get("age_s"),
            }
            print(dumps(notice) if args.json else notice["text"], flush=True)
        previous_health = health
        if reader.stream is not stream:
            stream, last = reader.stream, 0
        for event in snapshot["events"]:
            if event["sequence"] > last:
                print(dumps({"schema_version": 1, **event}) if args.json else event_line(event), flush=True)
                last = event["sequence"]
        if args.once or snapshot["stream_drained"]:
            return 0
        if not available(snapshot, "tail"):
            return 1
        time.sleep(reader.settings.stream_poll_s)
        snapshot = reader.snapshot(interactive=True, stream_batch=True, **options)


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.plain and args.json:
            raise ValueError("--plain and --json are mutually exclusive")
        query = getattr(args, "filter", "")
        terms(query)
        job = getattr(args, "job", None)
        if job is not None and not identifier(job.removeprefix("pack:")):
            raise ValueError("invalid Pack job ID")
        plan_id = getattr(args, "plan_id", None)
        if plan_id is not None and not identifier(plan_id):
            raise ValueError("invalid plan ID")
        settings = config(args)
    except (OSError, ValueError, RecursionError):
        # Configuration errors can carry secrets or untrusted path text.
        cli.error("invalid arguments or configuration")
    reader = Reader(settings)
    command = args.command or "jobs"
    try:
        if not (args.command or args.json or args.plain or args.once) and sys.stdin.isatty() and sys.stdout.isatty():
            if os.environ.get("TERM") not in (None, "", "dumb"):
                try:
                    import curses
                except ImportError:
                    pass
                else:
                    entered = False

                    def run(screen):
                        nonlocal entered
                        entered = True
                        return interface(screen, curses, reader)

                    try:
                        return curses.wrapper(run)
                    except curses.error:
                        if entered:
                            print("dashboard read failed", file=sys.stderr)
                            return 1
        if command == "tail":
            return tail(reader, args)
        snapshot = reader.snapshot(query=query, job_id=job, approval_status=getattr(args, "status", "all"),
                                   include_evidence=command == "approvals", include_gates=command in ("show", "gate"),
                                   plan_id=plan_id)
        if args.json:
            print(dumps(snapshot))
        else:
            for line in plain(snapshot, command):
                print(line)
        return 0 if available(snapshot, command) else 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError):
        print("dashboard read failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
