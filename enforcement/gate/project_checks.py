#!/usr/bin/env python3
# Header: Adapt deterministic checks to an installed project's component configuration.
# Exit codes: 0 check passed or no C/C++ changes; 1 check failed; 2 invalid configuration or usage.
"""Run changed-file quality checks and active component build/test commands."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Sequence
import xml.etree.ElementTree as ET

PACK_ROOT = Path(__file__).resolve().parents[2]
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from enforcement.checks.scope_check import get_changed_files  # noqa: E402
from enforcement.checks.test_proof import verify_gcov_proof  # noqa: E402
from runner.workspace_scope import ComponentSpec, resolve_workspace_scope  # noqa: E402

CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
_TEST_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})
_PACK_FRAMEWORK_RE = re.compile(r'#\s*include\s*"test_framework\.hpp"|\bcan_fixture_test::')


def _collect_component_test_sources(repo: Path, component: ComponentSpec) -> list[Path]:
    """Return C/C++ test source files under ``component`` or repo ``tests/``."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    for base in (component.abs_path / "tests", repo / "tests"):
        if not base.is_dir():
            continue
        for fp in sorted(base.rglob("*")):
            if fp.is_file() and fp.suffix.lower() in _TEST_SOURCE_SUFFIXES:
                res = fp.resolve()
                if res not in seen:
                    seen.add(res)
                    candidates.append(fp)
    return candidates


def _uses_pack_test_framework(repo: Path, component: ComponentSpec) -> bool:
    """Return True when the component or repo tests use the pack's ``test_framework.hpp``."""
    for base in (component.abs_path / "tests", repo / "tests"):
        if (base / "test_framework.hpp").is_file():
            return True
    for src in _collect_component_test_sources(repo, component):
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _PACK_FRAMEWORK_RE.search(text):
            return True
    return False


def _parse_junit_or_ctest_xml(xml_path: Path) -> tuple[bool, int, str]:
    """Validate a JUnit or CTest XML report and return ``(ok, executed_count, detail)``."""
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return False, 0, f"malformed test XML {xml_path.name}: {exc}"

    tag = root.tag.split("}")[-1]
    if tag in ("testsuites", "testsuite"):
        suites = [root] if tag == "testsuite" else list(root.findall(".//testsuite"))
        if not suites:
            cases = list(root.findall(".//testcase"))
            failures = len(root.findall(".//failure")) + len(root.findall(".//error"))
            skipped = len(root.findall(".//skipped"))
            executed = len(cases) - skipped
            if failures > 0 or executed <= 0:
                return False, 0, f"JUnit XML {xml_path.name} has executed={executed}, failures={failures}"
            return True, executed, f"JUnit XML {xml_path.name} ({executed} passed)"
        total_tests = 0
        total_failures = 0
        total_errors = 0
        total_skipped = 0
        for suite in suites:
            cases = suite.findall("testcase")
            t_attr = suite.get("tests")
            f_attr = suite.get("failures")
            e_attr = suite.get("errors")
            s_attr = suite.get("skipped") or suite.get("disabled")
            t_cnt = int(t_attr) if (t_attr is not None and t_attr.isdigit()) else len(cases)
            f_cnt = int(f_attr) if (f_attr is not None and f_attr.isdigit()) else (
                sum(1 for c in cases if c.find("failure") is not None)
            )
            e_cnt = int(e_attr) if (e_attr is not None and e_attr.isdigit()) else (
                sum(1 for c in cases if c.find("error") is not None)
            )
            s_cnt = int(s_attr) if (s_attr is not None and s_attr.isdigit()) else (
                sum(1 for c in cases if c.find("skipped") is not None)
            )
            total_tests += t_cnt
            total_failures += f_cnt
            total_errors += e_cnt
            total_skipped += s_cnt
        executed = total_tests - total_skipped
        if total_failures > 0 or total_errors > 0 or executed <= 0:
            return (
                False,
                0,
                f"JUnit XML {xml_path.name} invalid: executed={executed}, failures={total_failures}, errors={total_errors}",
            )
        return True, executed, f"JUnit XML {xml_path.name} ({executed} passed)"

    if tag == "Site":
        testing = root.find("Testing")
        if testing is None:
            return False, 0, f"CTest XML {xml_path.name} missing <Testing>"
        tests = testing.findall("Test")
        if not tests:
            return False, 0, f"CTest XML {xml_path.name} reports 0 tests"
        non_passed = [t.get("Status", "") for t in tests if t.get("Status") != "passed"]
        if non_passed:
            return False, 0, f"CTest XML {xml_path.name} has non-passing test(s): {non_passed}"
        return True, len(tests), f"CTest XML {xml_path.name} ({len(tests)} passed)"

    return False, 0, f"unrecognized XML root <{tag}> in {xml_path.name}"


def _find_recent_test_xmls(
    repo: Path,
    component: ComponentSpec,
    proof_dir: Path,
    start_time: float,
) -> list[Path]:
    """Locate JUnit or CTest XML files created or updated since ``start_time``."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    cutoff = start_time - 2.0

    def _maybe_add(p: Path) -> None:
        if p.is_file():
            try:
                res = p.resolve()
                if res not in seen and p.stat().st_mtime >= cutoff:
                    seen.add(res)
                    candidates.append(p)
            except OSError:
                pass

    env_xml = os.environ.get("DEVPACK_TEST_XML", "").strip()
    if env_xml:
        _maybe_add(Path(env_xml) if Path(env_xml).is_absolute() else repo / env_xml)

    for base in (proof_dir, component.abs_path, repo):
        if not base.is_dir():
            continue
        for fp in sorted(base.rglob("*.xml")):
            rel_parts = fp.relative_to(base).parts
            if ".git" in rel_parts:
                continue
            if base == proof_dir or fp.name == "Test.xml" or "junit" in fp.name.lower() or "test" in fp.name.lower():
                _maybe_add(fp)
    return candidates


def _find_gcov_obj_dir(repo: Path, component: ComponentSpec, proof_dir: Path, start_time: float) -> Path | None:
    """Locate the directory containing fresh ``.gcda`` files written during the test run."""
    cutoff = start_time - 2.0
    for cand in (proof_dir, component.abs_path, repo):
        if not cand.is_dir():
            continue
        for gcda in sorted(cand.rglob("*.gcda")):
            try:
                if gcda.stat().st_mtime >= cutoff:
                    return proof_dir if cand == proof_dir else gcda.parent
            except OSError:
                continue
    return None


def _verify_component_test_proof(
    repo: Path,
    component: ComponentSpec,
    proof_dir: Path,
    start_time: float,
    stdout_text: str,
) -> tuple[int, str]:
    """Verify out-of-process test proof for ``component`` after ``test_cmd`` exits 0."""
    if _uses_pack_test_framework(repo, component):
        test_sources = _collect_component_test_sources(repo, component)
        obj_dir = _find_gcov_obj_dir(repo, component, proof_dir, start_time)
        if obj_dir is None:
            return (
                1,
                f"FAIL: component '{component.name}' uses pack test framework but produced no gcov .gcda coverage data",
            )
        proof_res = verify_gcov_proof(
            test_sources,
            obj_dir,
            output_text=stdout_text,
            returncode=0,
            label=component.name,
        )
        if proof_res.exit_code == 3:
            return 3, proof_res.reason
        if proof_res.exit_code != 0:
            return 1, proof_res.reason
        return 0, f"PROOF_OK: {component.name} verified via gcov ({proof_res.verified_checks} assertion(s))"

    xml_files = _find_recent_test_xmls(repo, component, proof_dir, start_time)
    if not xml_files:
        return (
            1,
            f"FAIL: component '{component.name}' test_cmd exited 0 without JUnit/CTest XML result (test count > 0 required)",
        )
    valid_msgs: list[str] = []
    errors: list[str] = []
    for xml_fp in xml_files:
        ok, count, detail = _parse_junit_or_ctest_xml(xml_fp)
        if ok and count > 0:
            valid_msgs.append(detail)
        else:
            errors.append(detail)
    if errors and not valid_msgs:
        return 1, f"FAIL: component '{component.name}' test XML proof failed: {'; '.join(errors)}"
    if errors:
        return 1, f"FAIL: component '{component.name}' test XML contained failure/empty suite: {'; '.join(errors)}"
    return (
        0,
        f"PROOF_OK: {component.name} verified via {'; '.join(valid_msgs)} "
        "(note: XML proof is weaker than pack gcov proof; see policy/static_analysis.md)",
    )


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
                commands.append((component, ["bash", "-c", "set -euo pipefail\n" + command]))
            for component, command in commands:
                print(f"{args.check}: {component.name}: {json.dumps(command)}", flush=True)
                if not args.dry_run:
                    if args.check == "build":
                        result = subprocess.run(command, cwd=repo, env=environment, check=False)
                        if result.returncode:
                            return 1
                    else:
                        with tempfile.TemporaryDirectory(prefix="devpack-test-proof-") as proof_tmp:
                            proof_dir = Path(proof_tmp)
                            test_env = dict(environment)
                            test_env["DEVPACK_ROOT"] = str(PACK_ROOT)
                            test_env["DEVPACK_TEST_PROOF_DIR"] = str(proof_dir)
                            test_env["DEVPACK_GCOV_OBJ_DIR"] = str(proof_dir)
                            start_time = time.time()
                            result = subprocess.run(
                                command,
                                cwd=repo,
                                env=test_env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                check=False,
                            )
                            if result.stdout:
                                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
                            if result.returncode:
                                return 1
                            proof_rc, proof_msg = _verify_component_test_proof(
                                repo, component, proof_dir, start_time, result.stdout or ""
                            )
                            if proof_rc != 0:
                                print(proof_msg, file=sys.stderr)
                                return proof_rc
                            print(proof_msg, flush=True)
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
