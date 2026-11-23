#!/usr/bin/env python3
# Header: Adapt deterministic checks to an installed project's component configuration.
# Exit codes: 0 check passed or no C/C++ changes; 1 check failed; 2 invalid configuration or usage.
"""Run changed-file quality checks and active component build/test commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Sequence

PACK_ROOT = Path(__file__).resolve().parents[2]
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from enforcement.checks.scope_check import get_changed_files  # noqa: E402
from runner.workspace_scope import resolve_workspace_scope  # noqa: E402

CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the project check command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("lint", "cppcheck", "build", "tests"), required=True)
    parser.add_argument("--repo", default=".", help="Target project root, never the pack root.")
    parser.add_argument("--base", default=os.environ.get("DEVPACK_BASE", "HEAD"), help="Trusted base commit (default: DEVPACK_BASE or HEAD).")
    parser.add_argument("--project-config", default=None)
    parser.add_argument("--components", default=None)
    parser.add_argument("--all-components", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved commands without executing them.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute a project check against changed files or every active component."""
    args = build_arg_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        scope = resolve_workspace_scope(repo, project_config_path=args.project_config,
                                        components=args.components, all_components=args.all_components)
        if not scope.configured:
            raise ValueError("Project gate requires [workspace] and components in .devpack/project.toml")
        warning = scope.selection_warning()
        if warning:
            print(warning, flush=True)
        environment = dict(os.environ)
        if args.all_components:
            environment["DEVPACK_COMPONENTS"] = "all"
        elif args.components is not None:
            environment["DEVPACK_COMPONENTS"] = args.components
        if args.project_config:
            environment["DEVPACK_PROJECT_CONFIG"] = str(Path(args.project_config).resolve())
        if args.check in ("build", "tests"):
            if not scope.active_components:
                raise ValueError("NO_FILES_CHECKED: no active components")
            commands = []
            for component in scope.active_components:
                command = component.build_cmd if args.check == "build" else component.test_cmd
                if not command:
                    raise ValueError(f"Component '{component.name}' has no {'build_cmd' if args.check == 'build' else 'test_cmd'}")
                commands.append((component.name, ["bash", "-c", "set -euo pipefail\n" + command]))
            for name, command in commands:
                print(f"{args.check}: {name}: {json.dumps(command)}", flush=True)
                if not args.dry_run:
                    result = subprocess.run(command, cwd=repo, env=environment, check=False)
                    if result.returncode:
                        return 1
            return 0
        changed, error = get_changed_files(repo, args.base)
        if changed is None:
            raise ValueError(error)
        cpp_changes = [path for path in changed if Path(path).suffix.lower() in CPP_SUFFIXES]
        if not cpp_changes:
            print("SKIP: no C/C++ changes")
            return 0
        targets = [repo / path for path in scope.filter(cpp_changes) if (repo / path).is_file()]
        if not targets:
            raise ValueError("NO_FILES_CHECKED: changed C/C++ files are deleted, excluded, or outside active components")
        script = "enforcement/lint/preflight_lint.py" if args.check == "lint" else "enforcement/static/cppcheck_check.py"
        command = [sys.executable, str(PACK_ROOT / script), "--repo-root", str(repo)]
        if args.all_components:
            command.append("--all-components")
        if args.project_config:
            command.extend(["--project-config", args.project_config])
        with tempfile.TemporaryDirectory(prefix="devpack-project-check-") as temporary:
            if args.check == "lint":
                manifest = Path(temporary) / "changed.json"
                manifest.write_text(json.dumps(cpp_changes), encoding="utf-8")
                command.extend(["--changed-files", str(manifest)])
            command.extend(str(path) for path in targets)
            print(json.dumps(command), flush=True)
            if args.dry_run:
                return 0
            return 0 if subprocess.run(command, cwd=repo, env=environment, check=False).returncode == 0 else 1
    except (ValueError, OSError) as exc:
        print(f"PROJECT_CHECK_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
