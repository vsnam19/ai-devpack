#!/usr/bin/env python3
# Header: Adapt deterministic checks to an installed project's component configuration.
# Exit codes: 0 check passed or explicitly allowed empty changes; 1 check failed; 2 invalid configuration or usage.
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
from runner.safe_git import GitSecurityError, run_git
from runner.workspace_scope import resolve_workspace_scope  # noqa: E402

CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the project check command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("lint", "cppcheck", "build", "tests"), required=True)
    parser.add_argument("--repo", default=".", help="Target project root, never the pack root.")
    parser.add_argument("--base", default=None, help="Trusted base commit (default: DEVPACK_BASE or HEAD).")
    parser.add_argument("--project-config", default=None)
    parser.add_argument("--components", default=None)
    parser.add_argument("--all-components", action="store_true")
    parser.add_argument("--allow-empty", action="store_true", help="Explicitly allow no C/C++ changes.")
    parser.add_argument("--allow-unowned", action="store_true", help="Allow changed workspace files outside every component.")
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
        base = args.base or os.environ.get("DEVPACK_BASE", "").strip()
        ci_mode = os.environ.get("CI", "").lower() not in ("", "0", "false", "no")
        if ci_mode and not base:
            raise ValueError("DEVPACK_BASE is unset in CI; set DEVPACK_BASE to the trusted merge-base commit (or pass --base).")
        base = base or "HEAD"
        resolved = run_git(["rev-parse", "--verify", f"{base}^{{commit}}"], repo_root=repo, check=False)
        if resolved.returncode:
            raise ValueError(f"DEVPACK_BASE ref '{base}' cannot be resolved; git fetch --depth=100 origin <branch> (increase depth as needed), then set DEVPACK_BASE to the trusted merge base.")
        head = run_git(["rev-parse", "HEAD"], repo_root=repo, check=False)
        if ci_mode and resolved.stdout.strip() == head.stdout.strip():
            raise ValueError(f"DEVPACK_BASE '{base}' equals HEAD in CI; set DEVPACK_BASE to the trusted merge-base commit before these changes.")
        changed, error = get_changed_files(repo, base)
        if changed is None:
            raise ValueError(f"DEVPACK_BASE ref '{base}': {error}; git fetch --depth=100 origin <branch> and retry")
        cpp_changes = [path for path in changed if Path(path).suffix.lower() in CPP_SUFFIXES]
        if not cpp_changes:
            if not args.allow_empty:
                raise ValueError("No C/C++ changes against DEVPACK_BASE; set the correct base or explicitly pass --allow-empty.")
            print("SKIP: no C/C++ changes (--allow-empty)")
            return 0
        unowned = [path for path in cpp_changes
                   if not scope.outside_workspace(path) and not scope.is_excluded(path)
                   and not any((repo / path).resolve().is_relative_to(comp.abs_path)
                               for comp in scope.all_components)]
        if unowned:
            print("WARNING: changed C/C++ files outside every component: " + ", ".join(unowned) +
                  "; raise [workspace].discovery_depth or declare [[component]] entries.", flush=True)
            if not args.allow_unowned:
                raise ValueError("Unowned changes require explicit --allow-unowned")
        targets = [repo / path for path in scope.filter(cpp_changes) if (repo / path).is_file()]
        if not targets and args.allow_unowned and set(cpp_changes) <= set(unowned):
            print("SKIP: only explicitly allowed unowned changes")
            return 0
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
    except (ValueError, OSError, GitSecurityError) as exc:
        print(f"PROJECT_CHECK_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
