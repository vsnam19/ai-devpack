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
    if command == "tui":
        env = os.environ.copy()
        search_path = [str(ROOT)]
        if env.get("PYTHONPATH"):
            search_path.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(search_path)
        result = subprocess.run([*COMMANDS[command], *args], env=env)
    else:
        result = subprocess.run([*COMMANDS[command], *args])
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def doctor(workdir):
    from runner.agent_exec import load_project_config
    from runner.checkpoints import resolve_state_dir

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

    project = None
    try:
        if not workdir.is_dir():
            raise ValueError(f"Project directory is unavailable: {workdir}")
        project = load_project_config(workdir)
        if project["config_found"]:
            rows.append(("PASS", "Project config", str(workdir / ".devpack/project.toml")))
            check = subprocess.run(
                [*COMMANDS["skills"], "--check-config", project["engines_config"]],
                cwd=workdir, capture_output=True, text=True,
            )
            if check.returncode:
                rows.append(("FAIL", "Engine config", check.stderr.strip() or "Validation failed"))
            else:
                rows.append(("PASS", "Engine config", project["engines_config"]))
                for name, engine in json.loads(check.stdout)["engines"].items():
                    command = (engine.get("external") or {}).get("command") or engine.get("cmd")
                    binary = command[0] if command else os.environ.get("DEVPACK_AGENT_BIN", "claude")
                    candidate = str(workdir / binary) if "/" in binary else binary
                    path = shutil.which(candidate)
                    rows.append(("PASS" if path else "FAIL", f"Engine {name}",
                                 path or f"{binary} unavailable on host"))
        else:
            rows.append(("WARN", "Project config", "Run devpack init to create .devpack/project.toml"))
    except (OSError, ValueError) as exc:
        rows.append(("FAIL", "Project config", str(exc)))

    try:
        configured = project["pipeline"]["state_dir"] if project else None
        state_dir = resolve_state_dir(configured, project_root=workdir)
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


def init_project(args):
    workdir = args.workdir.resolve()
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
        if existing.returncode == 0 and os.path.lexists(workdir / existing.stdout.strip()):
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
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
