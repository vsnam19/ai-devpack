#!/usr/bin/env python3
# Header: Adapt deterministic checks to an installed project's component configuration.
# Exit codes: 0 check passed or explicitly allowed empty changes; 1 check failed; 2 invalid configuration or usage.
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
from enforcement.checks.symbol_check import (  # noqa: E402
    check_worker_objects,
    compile_source_to_object,
    compile_symbol_allowlist,
    load_symbol_allowlist_file,
)
from enforcement.checks.test_proof import clear_gcda_files, verify_gcov_proof  # noqa: E402
from runner.safe_git import GitSecurityError, run_git  # noqa: E402
from runner.workspace_scope import (  # noqa: E402
    ComponentSpec,
    WorkspaceScope,
    parse_toml_with_arrays,
    resolve_protected_test_globs,
    resolve_workspace_scope,
    validate_path_contained,
)

CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
_TEST_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})
_PACK_FRAMEWORK_RE = re.compile(r'#\s*include\s*"test_framework\.hpp"|\bcan_fixture_test::')
_XML_MTIME_SLACK_S = 0.05


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
            total_failures += max(f_cnt, sum(1 for c in cases if c.find("failure") is not None))
            total_errors += max(e_cnt, sum(1 for c in cases if c.find("error") is not None))
            total_skipped += s_cnt
        all_cases = list(root.findall(".//testcase"))
        executed = total_tests - total_skipped
        if total_failures > 0 or total_errors > 0:
            return (
                False,
                0,
                f"JUnit XML {xml_path.name} invalid: executed={executed}, failures={total_failures}, errors={total_errors}",
            )
        if not all_cases or executed <= 0:
            return (
                False,
                0,
                f"JUnit XML {xml_path.name} invalid: executed=0 (testcases={len(all_cases)})",
            )
        return True, len(all_cases), f"JUnit XML {xml_path.name} ({len(all_cases)} passed)"

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


def _select_project_config_file(repo: Path, project_config_path: str | None) -> Path | None:
    """Locate the active project TOML file using the same precedence as workspace_scope."""
    selected = project_config_path or os.environ.get("DEVPACK_PROJECT_CONFIG")
    if selected is not None and str(selected).lower() == "none":
        return None
    if selected is not None and str(selected):
        return Path(selected)
    default_cfg = repo / ".devpack" / "project.toml"
    return default_cfg if default_cfg.is_file() else None


def _normalize_relative_subpath(raw_path: str, *, field_name: str) -> str:
    """Validate and normalize a relative path that does not escape its base with '..'."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
        raise ValueError(f"{field_name} must be relative, got absolute path: {raw_path!r}")
    norm = os.path.normpath(cleaned).replace("\\", "/")
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"{field_name} must not escape with '..': {raw_path!r}")
    return norm


def _resolve_component_build_and_xml(
    repo: Path,
    scope: WorkspaceScope,
    project_config_path: str | None,
) -> tuple[dict[str, Path], dict[str, Path | None]]:
    """Resolve per-component build dirs and declared ``test_xml`` paths; reject duplicates."""
    cfg_path = _select_project_config_file(repo, project_config_path)
    raw_entries: dict[str, dict[str, object]] = {}
    if cfg_path is not None and cfg_path.is_file():
        parsed = parse_toml_with_arrays(cfg_path.read_text(encoding="utf-8"))
        for entry in parsed.get("component") or ():
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                raw_entries[entry["name"].strip()] = entry

    build_dirs: dict[str, Path] = {}
    xml_paths: dict[str, Path | None] = {}
    claimed_xmls: dict[Path, str] = {}

    for comp in scope.all_components:
        entry = raw_entries.get(comp.name, {})
        raw_build_dir = entry.get("build_dir", "")
        if raw_build_dir is None:
            raw_build_dir = ""
        if not isinstance(raw_build_dir, str):
            raise ValueError(f"[[component]] '{comp.name}'.build_dir must be a string")

        if raw_build_dir.strip():
            norm_bd = _normalize_relative_subpath(
                raw_build_dir, field_name=f"[[component]] '{comp.name}'.build_dir"
            )
            comp_build_dir = validate_path_contained(
                repo / norm_bd, repo, label=f"[[component]] '{comp.name}'.build_dir"
            )
        elif comp.compile_commands:
            comp_build_dir = validate_path_contained(
                (repo / comp.compile_commands).parent,
                repo,
                label=f"[[component]] '{comp.name}'.compile_commands parent",
            )
        else:
            comp_build_dir = comp.abs_path.resolve()
        build_dirs[comp.name] = comp_build_dir

        raw_xml = entry.get("test_xml", "")
        if raw_xml is None:
            raw_xml = ""
        if not isinstance(raw_xml, str):
            raise ValueError(f"[[component]] '{comp.name}'.test_xml must be a string")

        if raw_xml.strip():
            norm_xml = _normalize_relative_subpath(
                raw_xml, field_name=f"[[component]] '{comp.name}'.test_xml"
            )
            if norm_xml == ".":
                raise ValueError(f"[[component]] '{comp.name}'.test_xml must name a file, not '.'")
            resolved_xml = validate_path_contained(
                comp_build_dir / norm_xml,
                repo,
                label=f"[[component]] '{comp.name}'.test_xml",
            )
            owner = claimed_xmls.get(resolved_xml)
            if owner is not None:
                raise ValueError(
                    f"Duplicate test_xml path '{resolved_xml}' configured for both '{owner}' and '{comp.name}'"
                )
            claimed_xmls[resolved_xml] = comp.name
            xml_paths[comp.name] = resolved_xml
        else:
            xml_paths[comp.name] = None

    return build_dirs, xml_paths


def _resolve_component_symbol_allowlists(
    repo: Path,
    scope: WorkspaceScope,
    project_config_path: str | None,
) -> dict[str, tuple[tuple[str, ...], Path | None]]:
    """Resolve optional production undefined-symbol allowlists from project TOML."""
    cfg_path = _select_project_config_file(repo, project_config_path)
    parsed: dict[str, object] = {}
    raw_entries: dict[str, dict[str, object]] = {}
    if cfg_path is not None and cfg_path.is_file():
        parsed = parse_toml_with_arrays(cfg_path.read_text(encoding="utf-8"))
        for entry in parsed.get("component") or ():
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                raw_entries[entry["name"].strip()] = entry

    def _extract_allowlist(table: dict[str, object], label_prefix: str) -> tuple[tuple[str, ...], Path | None]:
        raw_list = table.get("symbol_allowlist")
        if raw_list is None:
            raw_list = table.get("allowlist")
        patterns: tuple[str, ...] = ()
        if raw_list is not None:
            if not isinstance(raw_list, list) or any(not isinstance(item, str) for item in raw_list):
                raise ValueError(f"{label_prefix}.symbol_allowlist must be a list of regex strings")
            patterns = tuple(item.strip() for item in raw_list if item.strip())
            compile_symbol_allowlist(patterns)

        raw_file = table.get("symbol_allowlist_file")
        if raw_file is None:
            raw_file = table.get("allowlist_file")
        allow_file: Path | None = None
        if raw_file is not None:
            if not isinstance(raw_file, str):
                raise ValueError(f"{label_prefix}.symbol_allowlist_file must be a relative path string")
            if raw_file.strip():
                norm_file = _normalize_relative_subpath(
                    raw_file, field_name=f"{label_prefix}.symbol_allowlist_file"
                )
                allow_file = validate_path_contained(
                    repo / norm_file, repo, label=f"{label_prefix}.symbol_allowlist_file"
                )
                load_symbol_allowlist_file(allow_file)
        return patterns, allow_file

    ws_table = parsed.get("workspace") if isinstance(parsed.get("workspace"), dict) else {}
    sym_table = parsed.get("symbols") if isinstance(parsed.get("symbols"), dict) else {}
    default_patterns, default_file = _extract_allowlist(
        {**ws_table, **sym_table}, "[symbols]" if sym_table else "[workspace]"
    )

    result: dict[str, tuple[tuple[str, ...], Path | None]] = {}
    for comp in scope.all_components:
        entry = raw_entries.get(comp.name, {})
        comp_patterns, comp_file = _extract_allowlist(entry, f"[[component]] '{comp.name}'")
        merged_patterns = comp_patterns if comp_patterns else default_patterns
        chosen_file = comp_file if comp_file is not None else default_file
        result[comp.name] = (merged_patterns, chosen_file)
    return result


def _find_gcov_obj_dir(
    component: ComponentSpec,
    build_dir: Path,
    proof_dir: Path,
    start_time: float,
) -> Path | None:
    """Locate fresh ``.gcda`` files inside ``proof_dir``, ``build_dir``, or ``component``."""
    cutoff = start_time - _XML_MTIME_SLACK_S
    seen: set[Path] = set()
    for cand in (proof_dir, build_dir, component.abs_path):
        resolved_cand = cand.resolve()
        if resolved_cand in seen or not resolved_cand.is_dir():
            continue
        seen.add(resolved_cand)
        for gcda in sorted(resolved_cand.rglob("*.gcda")):
            try:
                if gcda.stat().st_mtime >= cutoff:
                    return proof_dir if cand == proof_dir else gcda.parent
            except OSError:
                continue
    return None


def _is_component_test_path(fp: Path, repo: Path, component: ComponentSpec) -> bool:
    """Return True if ``fp`` sits in a test directory or follows test file naming."""
    try:
        rel = fp.resolve().relative_to(repo.resolve()).parts
    except ValueError:
        rel = fp.parts
    if any(p in ("tests", "test", "hidden", "hidden_tests") for p in rel[:-1]):
        return True
    name = fp.name
    return name.startswith("test_") or name.endswith("_test.cpp") or name.endswith("_test.c")


def _run_component_symbol_check(
    repo: Path,
    component: ComponentSpec,
    build_dir: Path,
    scope: WorkspaceScope,
    *,
    base_ref: str,
    uses_pack_fw: bool,
    import_allowlist: Sequence[str] = (),
    import_allowlist_file: Path | None = None,
) -> tuple[int, str]:
    """Compile worker-changed/component C/C++ sources and run ``symbol_check.py`` before tests."""
    inc_dirs: list[Path] = []
    for raw_inc in component.include_dirs:
        p = Path(raw_inc)
        cand = p if p.is_absolute() else (component.abs_path / p if (component.abs_path / p).is_dir() else repo / p)
        if cand.is_dir() and cand not in inc_dirs:
            inc_dirs.append(cand)
    for fallback_inc in (component.abs_path / "include", repo / "include", component.abs_path / "tests", repo / "tests"):
        if fallback_inc.is_dir() and fallback_inc not in inc_dirs:
            inc_dirs.append(fallback_inc)

    changed_sources: list[Path] = []
    header_changed = False
    comp_resolved = component.abs_path.resolve()
    resolved_inc_dirs = tuple(d.resolve() for d in inc_dirs)
    if base_ref:
        try:
            changed, _err = get_changed_files(repo, base_ref)
        except GitSecurityError:
            changed = None
        if changed:
            for rel in changed:
                cand = (repo / rel).resolve()
                if scope.is_excluded(cand):
                    continue
                in_comp = cand.is_relative_to(comp_resolved) or any(
                    cand.is_relative_to(inc_d) for inc_d in resolved_inc_dirs
                )
                if cand.suffix.lower() in _HEADER_SUFFIXES and in_comp:
                    header_changed = True
                    continue
                if (
                    cand.is_file()
                    and cand.suffix.lower() in _TEST_SOURCE_SUFFIXES
                    and cand.is_relative_to(comp_resolved)
                ):
                    changed_sources.append(cand)

    worker_test_srcs = [
        fp
        for fp in changed_sources
        if _is_component_test_path(fp, repo, component)
        and fp.name not in ("test_main.cpp", "test_can_codec.cpp", "test_ring_buffer.cpp", "test_link_state_machine.cpp", "test_dtc_debouncer.cpp")
    ]

    if header_changed or (not changed_sources and component.abs_path.is_dir()):
        worker_srcs: list[Path] = []
        if component.abs_path.is_dir():
            for fp in sorted(component.abs_path.rglob("*")):
                if not fp.is_file() or fp.suffix.lower() not in _TEST_SOURCE_SUFFIXES:
                    continue
                if scope.is_excluded(fp):
                    continue
                if any(part == ".git" or part.startswith("build") for part in fp.parts):
                    continue
                if not _is_component_test_path(fp, repo, component):
                    worker_srcs.append(fp)
    else:
        worker_srcs = [fp for fp in changed_sources if not _is_component_test_path(fp, repo, component)]

    if not worker_srcs and not worker_test_srcs:
        return 0, ""

    cc_path = (repo / component.compile_commands) if component.compile_commands else None
    fw_hdr = None
    for cand_fw in (component.abs_path / "tests" / "test_framework.hpp", repo / "tests" / "test_framework.hpp"):
        if cand_fw.is_file():
            fw_hdr = cand_fw
            break

    with tempfile.TemporaryDirectory(prefix="devpack-proj-sym-") as sym_tmp:
        tmp_path = Path(sym_tmp)
        worker_objs: list[Path] = []
        worker_test_objs: list[Path] = []
        framework_objs: list[Path] = []

        for idx, src in enumerate(worker_srcs, start=1):
            obj = tmp_path / f"w_{idx}_{src.stem}.o"
            ok, err = compile_source_to_object(
                src, obj, include_dirs=inc_dirs, compile_commands=cc_path
            )
            if ok:
                worker_objs.append(obj)
            else:
                # Fall back to freshly built object in component build_dir if present (e.g. external SDK headers)
                existing_o = next(
                    (
                        p
                        for p in sorted(build_dir.rglob(f"{src.stem}*.o"))
                        if p.is_file() and p.stat().st_mtime >= src.stat().st_mtime
                    ),
                    None,
                ) if build_dir.is_dir() else None
                if existing_o is not None:
                    worker_objs.append(existing_o)
                elif err.startswith("TOOL_MISSING:"):
                    return 3, err
                else:
                    return 1, f"FAIL: symbol_check compile failed for {component.name}/{src.name}: {err}"

        for idx, src in enumerate(worker_test_srcs, start=1):
            obj = tmp_path / f"wt_{idx}_{src.stem}.o"
            extra = ["-include", str(fw_hdr)] if (uses_pack_fw and fw_hdr is not None) else []
            ok, err = compile_source_to_object(
                src, obj, include_dirs=inc_dirs, compile_commands=cc_path, extra_flags=extra
            )
            if ok:
                worker_test_objs.append(obj)
            elif err.startswith("TOOL_MISSING:"):
                return 3, err
            else:
                return 1, f"FAIL: symbol_check compile failed for {component.name}/{src.name}: {err}"

        if uses_pack_fw:
            for main_cand in (component.abs_path / "tests" / "test_main.cpp", repo / "tests" / "test_main.cpp"):
                if main_cand.is_file():
                    fw_obj = tmp_path / "fw_test_main.o"
                    extra = ["-include", str(fw_hdr)] if fw_hdr is not None else []
                    ok, _ = compile_source_to_object(
                        main_cand, fw_obj, include_dirs=inc_dirs, compile_commands=cc_path, extra_flags=extra
                    )
                    if ok:
                        framework_objs.append(fw_obj)
                    break

        res = check_worker_objects(
            worker_objs,
            worker_test_objs=worker_test_objs,
            framework_objs=framework_objs,
            import_profile="production",
            import_allowlist=import_allowlist,
            import_allowlist_file=import_allowlist_file,
        )
        if res.exit_code == 3:
            return 3, "; ".join(res.errors)
        if res.exit_code != 0:
            details = "; ".join(v.reason for v in res.violations) or "; ".join(res.errors)
            return 1, f"FAIL: component '{component.name}' symbol_check failed: {details}"
    return 0, ""


def _verify_component_test_proof(
    repo: Path,
    component: ComponentSpec,
    build_dir: Path,
    xml_path: Path | None,
    proof_dir: Path,
    start_time: float,
    stdout_text: str,
) -> tuple[int, str]:
    """Verify out-of-process test proof for ``component`` after ``test_cmd`` exits 0."""
    if _uses_pack_test_framework(repo, component):
        test_sources = _collect_component_test_sources(repo, component)
        obj_dir = _find_gcov_obj_dir(component, build_dir, proof_dir, start_time)
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
            min_mtime=start_time,
        )
        if proof_res.exit_code == 3:
            return 3, proof_res.reason
        if proof_res.exit_code != 0:
            return 1, proof_res.reason
        return 0, f"PROOF_OK: {component.name} verified via gcov ({proof_res.verified_checks} assertion(s))"

    if xml_path is None:
        return (
            1,
            f"FAIL: component '{component.name}' has no 'test_xml' configured in .devpack/project.toml",
        )
    if xml_path.is_symlink() or not xml_path.is_file():
        return (
            1,
            f"FAIL: component '{component.name}' test_cmd exited 0 without creating declared test_xml '{xml_path}'",
        )
    try:
        xml_mtime = xml_path.stat().st_mtime
    except OSError as exc:
        return 1, f"FAIL: component '{component.name}' unable to stat test_xml '{xml_path}': {exc}"
    if xml_mtime < (start_time - _XML_MTIME_SLACK_S):
        return (
            1,
            f"FAIL: component '{component.name}' test_xml '{xml_path}' is stale (mtime older than test start)",
        )

    ok, count, detail = _parse_junit_or_ctest_xml(xml_path)
    if not ok or count <= 0:
        return 1, f"FAIL: component '{component.name}' test XML proof failed: {detail}"
    return (
        0,
        f"PROOF_OK: {component.name} verified via {detail} "
        "(note: XML proof is weaker than pack gcov proof; see policy/static_analysis.md)",
    )


def _resolve_base_commit(repo: Path, raw_base: str | None) -> str:
    """Resolve and validate the base commit SHA, enforcing CI non-HEAD requirements."""
    base = raw_base or os.environ.get("DEVPACK_BASE", "").strip()
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
    return base


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the project check command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("lint", "cppcheck", "test_lock", "build", "tests"), required=True)
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
        if args.check == "test_lock":
            base = _resolve_base_commit(repo, args.base)
            protected_globs = resolve_protected_test_globs(
                repo, project_config_path=args.project_config
            )
            command = [
                sys.executable,
                str(PACK_ROOT / "enforcement/checks/test_lock.py"),
                "--repo",
                str(repo),
                "--base",
                base,
                "--no-deviation",
                "--protected",
                *protected_globs,
            ]
            print(json.dumps(command), flush=True)
            if args.dry_run:
                return 0
            return 0 if subprocess.run(command, cwd=repo, env=environment, check=False).returncode == 0 else 1

        if args.check in ("build", "tests"):
            if not scope.active_components:
                raise ValueError("NO_FILES_CHECKED: no active components")
            build_dirs, xml_paths = _resolve_component_build_and_xml(
                repo, scope, args.project_config
            )
            symbol_allowlists = (
                _resolve_component_symbol_allowlists(repo, scope, args.project_config)
                if args.check == "tests"
                else {}
            )
            commands = []
            for component in scope.active_components:
                command = component.build_cmd if args.check == "build" else component.test_cmd
                if not command:
                    raise ValueError(f"Component '{component.name}' has no {'build_cmd' if args.check == 'build' else 'test_cmd'}")
                commands.append((component, ["bash", "-c", "set -euo pipefail\n" + command]))
            raw_base_opt = args.base or os.environ.get("DEVPACK_BASE", "").strip()
            for component, command in commands:
                print(f"{args.check}: {component.name}: {json.dumps(command)}", flush=True)
                if not args.dry_run:
                    if args.check == "build":
                        result = subprocess.run(command, cwd=repo, env=environment, check=False)
                        if result.returncode:
                            return 1
                    else:
                        comp_build_dir = build_dirs[component.name]
                        comp_xml = xml_paths.get(component.name)
                        uses_fw = _uses_pack_test_framework(repo, component)

                        comp_allow_pats, comp_allow_file = symbol_allowlists.get(component.name, ((), None))
                        sym_rc, sym_msg = _run_component_symbol_check(
                            repo,
                            component,
                            comp_build_dir,
                            scope,
                            base_ref=raw_base_opt,
                            uses_pack_fw=uses_fw,
                            import_allowlist=comp_allow_pats,
                            import_allowlist_file=comp_allow_file,
                        )
                        if sym_rc != 0:
                            print(sym_msg, file=sys.stderr)
                            return sym_rc

                        if comp_xml is not None:
                            if comp_xml.is_dir():
                                raise ValueError(
                                    f"[[component]] '{component.name}'.test_xml points to a directory: {comp_xml}"
                                )
                            if comp_xml.is_symlink() or comp_xml.exists():
                                comp_xml.unlink()

                        with tempfile.TemporaryDirectory(prefix="devpack-test-proof-") as proof_tmp:
                            proof_dir = Path(proof_tmp)
                            clear_gcda_files(comp_build_dir)
                            test_env = dict(environment)
                            test_env["DEVPACK_ROOT"] = str(PACK_ROOT)
                            test_env["DEVPACK_TEST_PROOF_DIR"] = str(proof_dir)
                            test_env["DEVPACK_GCOV_OBJ_DIR"] = str(proof_dir)
                            if comp_xml is not None:
                                test_env["DEVPACK_TEST_XML"] = str(comp_xml)
                            else:
                                test_env.pop("DEVPACK_TEST_XML", None)
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
                                repo, component, comp_build_dir, comp_xml, proof_dir, start_time, result.stdout or ""
                            )
                            if proof_rc != 0:
                                print(proof_msg, file=sys.stderr)
                                return proof_rc
                            print(proof_msg, flush=True)
            return 0
        base = _resolve_base_commit(repo, args.base)
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
