#!/usr/bin/env python3
"""One entry point for the installed devpack tools."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {
    name: (sys.executable, str(ROOT / "runner" / f"devpack_{name}.py"))
    for name in ("run", "plan", "dispatch", "broker", "approve", "logs", "dash")
}
COMMANDS.update(
    tui=(sys.executable, "-m", "runner.dash.tui"),
    gate=(sys.executable, str(ROOT / "enforcement/gate/gate.py")),
    skills=(sys.executable, str(ROOT / "runner/agent_exec.py")),
    hooks=("bash", str(ROOT / "enforcement/hooks/install.sh")),
    selftest=("bash", str(ROOT / "tests/run_all.sh")),
)


def forward(command, args):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if command == "tui":
        search_path = [str(ROOT)]
        if env.get("PYTHONPATH"):
            search_path.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(search_path)
    result = subprocess.run([*COMMANDS[command], *args], env=env)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def _validate_engine_entry(name, engine, workdir, data_class):
    """Validate host availability, data_class policy, and local env vars for one engine."""
    from runner.agent_exec import EngineConfig, HarnessError, _resolve_local_engine_env, check_data_class_allowed

    issues = []
    command = (engine.get("external") or {}).get("command") or engine.get("cmd")
    binary = command[0] if command else os.environ.get("DEVPACK_AGENT_BIN", "claude")
    candidate = str(workdir / binary) if "/" in binary else binary
    path = shutil.which(candidate)
    if not path:
        issues.append(f"{binary} unavailable on host")

    eng_class = str(engine.get("class") or engine.get("engine_class") or "cloud").strip()
    eng_cfg = EngineConfig(
        name=name,
        model=str(engine.get("model") or name),
        engine_class=eng_class,
        max_data_class=str(engine.get("max_data_class") or "internal").strip(),
        base_url_env=str(engine.get("base_url_env") or "").strip(),
        auth_token_env=str(engine.get("auth_token_env") or "").strip(),
    )
    dc_ok, dc_reason = check_data_class_allowed(eng_cfg, data_class)
    if not dc_ok:
        issues.append(dc_reason)

    if eng_class == "local":
        try:
            _resolve_local_engine_env(eng_cfg, os.environ)
        except HarnessError as exc:
            issues.append(str(exc))

    if issues:
        return "FAIL", "; ".join(issues)
    return "PASS", path


def doctor(workdir):
    from runner.agent_exec import load_project_config
    from runner.checkpoints import load_pipeline_config

    rows = [("PASS", "Python", sys.version.split()[0])]
    for label, binary, missing in (
        ("Git", "git", "FAIL"),
        ("Docker", "docker", "WARN"),
        ("ShellCheck (selftest)", "shellcheck", "FAIL"),
    ):
        path = shutil.which(binary)
        rows.append(("PASS" if path else missing, label, path or "Unavailable on PATH"))
    textual = importlib.util.find_spec("textual") is not None
    rows.append(("PASS" if textual else "WARN", "Textual", "Available" if textual else "Optional for tui"))

    workdir_ok = workdir.is_dir()
    project = None
    if not workdir_ok:
        rows.append(("FAIL", "Project config", f"Project directory is unavailable: {workdir}"))
    else:
        try:
            project_toml = workdir / ".devpack/project.toml"
            if project_toml.is_symlink():
                raise ValueError(f"Project config must not be a symlink: {project_toml}")
            project = load_project_config(workdir)
            if project["config_found"]:
                rows.append(("PASS", "Project config", str(project_toml)))
                raw_gate = str(project.get("gate_config", "")).strip()
                if not raw_gate:
                    rows.append(("FAIL", "Gate config", "Project gate_config is unset in .devpack/project.toml"))
                else:
                    gate_path = Path(raw_gate)
                    if gate_path.is_symlink():
                        rows.append(("FAIL", "Gate config", f"Project gate_config must not be a symlink: {gate_path}"))
                    elif not gate_path.is_file():
                        rows.append(("FAIL", "Gate config", f"Project gate_config not found: {gate_path}"))
                    else:
                        rows.append(("PASS", "Gate config", str(gate_path)))

                check = subprocess.run(
                    [*COMMANDS["skills"], "--check-config", project["engines_config"]],
                    cwd=workdir,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    capture_output=True,
                    text=True,
                )
                if check.returncode:
                    rows.append(("FAIL", "Engine config", check.stderr.strip() or "Validation failed"))
                else:
                    rows.append(("PASS", "Engine config", project["engines_config"]))
                    payload = json.loads(check.stdout)
                    engines_map = payload.get("engines", {})
                    roles_map = payload.get("roles") or {}
                    missing_roles = [role for role in ("implementer", "reviewer") if role not in roles_map]
                    if missing_roles:
                        rows.append(("FAIL", "Engine roles", f"Missing required role(s): {', '.join(missing_roles)}"))
                    else:
                        rows.append((
                            "PASS",
                            "Engine roles",
                            f"implementer={roles_map['implementer']}, reviewer={roles_map['reviewer']}",
                        ))
                    for role, target in (project.get("default_engines") or {}).items():
                        if target not in engines_map:
                            rows.append(("FAIL", f"Default engine {role}", f"References undefined engine '{target}'"))
                        else:
                            rows.append(("PASS", f"Default engine {role}", target))
                    for name, engine in engines_map.items():
                        status, detail = _validate_engine_entry(name, engine, workdir, project["data_class"])
                        rows.append((status, f"Engine {name}", detail))
            else:
                rows.append(("WARN", "Project config", "Run devpack init to create .devpack/project.toml"))
        except (OSError, ValueError) as exc:
            rows.append(("FAIL", "Project config", str(exc)))

    if not workdir_ok:
        rows.append(("FAIL", "State directory", f"Project directory is unavailable: {workdir}"))
    else:
        try:
            pipeline_cfg = load_pipeline_config(workdir)
            state_dir = Path(pipeline_cfg.state_dir)
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=state_dir) as probe:
                probe.write(b"devpack")
                probe.flush()
            rows.append(("PASS", "State directory", str(state_dir)))
        except (OSError, ValueError) as exc:
            rows.append(("FAIL", "State directory", str(exc)))

    print(f"{'STATUS':<7} {'CHECK':<24} DETAILS")
    for status, label, detail in rows:
        print(f"{status:<7} {label:<24} {detail}")
    return int(any(status == "FAIL" for status, _, _ in rows))


def create_file(path, content, workdir):
    if not path.parent.resolve().is_relative_to(workdir):
        raise ValueError(f"Path escapes the project: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        print(f"Exists  {path.relative_to(workdir)}")
    else:
        print(f"Created {path.relative_to(workdir)}")


def _require_git_repo_root(workdir):
    """Verify ``workdir`` is itself the top-level directory of a Git repository."""
    if not workdir.is_dir():
        raise ValueError(f"--install-hooks requires an existing git repository root: {workdir}")
    top = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if top.returncode != 0:
        raise ValueError(f"--install-hooks requires a git repository root: {workdir}")
    toplevel = Path(top.stdout.rstrip("\r\n")).resolve()
    if toplevel != workdir:
        raise ValueError(
            f"--install-hooks requires a git repository root (got {workdir}, repo root is {toplevel})"
        )


def init_project(args):
    workdir = args.workdir.resolve()
    if args.install_hooks:
        _require_git_repo_root(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    config = f'''data_class = "{args.data_class}"
engines_config = "engines.toml"
gate_config = "gate.toml"
plans_root = "plans"

[pipeline]
mode = "{args.mode}"
'''
    create_file(workdir / ".devpack/project.toml", config.encode(), workdir)
    for source, destination in (
        ("runner/engines.example.toml", ".devpack/engines.toml"),
        ("enforcement/gate/gate.project.example.toml", ".devpack/gate.toml"),
    ):
        create_file(workdir / destination, (ROOT / source).read_bytes(), workdir)
    template = ROOT / "workflow/templates/plan.example"
    for source in sorted(template.rglob("*")):
        if source.is_file():
            destination = workdir / "plans/example" / source.relative_to(template)
            create_file(destination, source.read_bytes(), workdir)

    ignore = workdir / ".gitignore"
    if ignore.is_symlink():
        print("Exists  .gitignore (symlink, add /runs/ in the project)")
    else:
        with ignore.open("a+b") as stream:
            stream.seek(0)
            content = stream.read()
            if b"/runs/" in content.splitlines():
                print("Exists  /runs/ in .gitignore")
            else:
                separator = b"\n" if content and not content.endswith(b"\n") else b""
                stream.write(separator + b"/runs/\n")
                print("Added   /runs/ to .gitignore")

    if args.install_hooks:
        existing = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--git-path", "hooks/pre-commit"],
            capture_output=True, text=True,
        )
        hook_rel = existing.stdout.rstrip("\r\n")
        if existing.returncode == 0 and hook_rel and os.path.lexists(workdir / hook_rel):
            print("Exists  pre-commit hook")
        else:
            status = forward("hooks", ["--repo", str(workdir)])
            if status:
                return status
    if args.install_skills:
        return forward("skills", ["--install-skills", str(workdir)])
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < (3, 10):
        print(f"FAIL Python 3.10 or newer is required (found {sys.version.split()[0]}).", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(prog="devpack", description=__doc__)
    parser.add_argument("command", choices=(*("version", "doctor", "init"), *COMMANDS))
    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    command = parser.parse_args(argv[:1]).command
    remaining = argv[1:]
    try:
        if command in COMMANDS:
            return forward(command, remaining)
        options = argparse.ArgumentParser(prog=f"devpack {command}")
        if command == "version":
            options.parse_args(remaining)
            print((ROOT / "VERSION").read_text().strip())
            return 0
        options.add_argument("--workdir", type=Path, default=Path.cwd())
        if command == "doctor":
            return doctor(options.parse_args(remaining).workdir.resolve())

        from runner.agent_exec import DATA_CLASS_ORDER

        options.add_argument("--mode", choices=("hitl", "autonomous"), default="hitl")
        options.add_argument("--data-class", choices=DATA_CLASS_ORDER, default="internal")
        options.add_argument("--install-hooks", action="store_true")
        options.add_argument("--install-skills", action="store_true")
        return init_project(options.parse_args(remaining))
    except (OSError, ValueError) as exc:
        print(f"devpack {command}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
