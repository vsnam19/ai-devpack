#!/usr/bin/env python3
# Header: Out-of-process test execution proof via gcov line coverage on test TUs.
#
# Usage:
#   python3 enforcement/checks/test_proof.py run --test-src FILE [...] --obj-dir DIR [--assert-regex RX] [--label NAME] -- CMD [ARGS...]
#   python3 enforcement/checks/test_proof.py verify --test-src FILE [...] --obj-dir DIR [--stdout-file FILE] [--returncode N]
#
# Compiles/verifies test translation units built with --coverage (gcov),
# requires gcov on PATH (exit 3 TOOL_MISSING if absent), runs the test command
# with DEVPACK_HARNESS_NONCE* scrubbed from the environment, requires normal
# exit status 0 with no failure lines, and parses gcov -j/-i JSON output to
# prove every CHECK* assertion line (and main completion summary when present)
# in the pristine test sources was executed at least once.
#
# Exit codes:
#   0 - Test run exited 0 and gcov proved all assertion lines executed
#   1 - Test run or out-of-process gcov proof failed
#   2 - Invalid CLI arguments or internal usage error
#   3 - TOOL_MISSING (gcov executable not found on PATH)
"""Out-of-process gcov test coverage proof verifier for C/C++ test binaries."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Sequence

DEFAULT_ASSERT_REGEX = r"(?<![A-Za-z0-9_])CHECK(?:_EQ|_TRUE|_FALSE|_NEAR)?\s*\("
FAILURE_PATH_MARKER = "DEVPACK_FAILURE_PATH"
SUCCESS_PATH_MARKER = "DEVPACK_SUCCESS_PATH"
CHECK_TOTAL_MARKER = "DEVPACK_CHECK_TOTAL"
_FAILURE_PATH_RE = re.compile(rf"//[^\n]*\b{FAILURE_PATH_MARKER}\b")
_SUCCESS_PATH_RE = re.compile(rf"//[^\n]*\b{SUCCESS_PATH_MARKER}\b")
_CHECK_TOTAL_RE = re.compile(rf"(?://[^\n]*\b{CHECK_TOTAL_MARKER}\b|\+\+\s*state\.totalChecks\b)")
_FAILURE_LINE_RE = re.compile(
    r"(?m)^(?:\s*FAIL:|.*:\s*CHECK(?:_EQ|_TRUE|_FALSE|_NEAR)?\([^)]*\)\s+failed|.*GRADER_CHECK\s+failed)"
)
_COMPLETION_LINE_RE = re.compile(r"\bprintSummaryAndExitCode\s*(?:<[^>]*>)?\s*\(|DEVPACK_TESTS_COMPLETED")
_COMPLETED_TALLY_RE = re.compile(r"DEVPACK_TESTS_COMPLETED\s+checks=(\d+)\s+failed=(\d+)")
_FRAMEWORK_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"[^"]*test_framework\.hpp"', re.MULTILINE)
_MTIME_JIFFY_SLACK_S = 0.05


@dataclass(frozen=True)
class CheckLineSpec:
    """Line span of a single assertion macro invocation in a pristine test file."""

    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class ProofResult:
    """Outcome of running or verifying an out-of-process gcov test proof."""

    exit_code: int
    reason: str = ""
    output: str = ""
    verified_checks: int = 0


def find_gcov_bin(gcov_bin: str = "gcov") -> str | None:
    """Return resolved path to ``gcov`` executable, or None if unavailable."""
    if os.sep in gcov_bin or (os.altsep and os.altsep in gcov_bin):
        cand = Path(gcov_bin)
        return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None
    return shutil.which(gcov_bin)


def _strip_comments_and_strings_preserve_lines(text: str) -> str:
    """Replace C/C++ comments and string/char literals with spaces, preserving newlines."""
    rx = re.compile(
        r'R"(?P<delim>[^ ()\\\t\r\n]{0,16})\(.*?\)(?P=delim)"'
        r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
        r"|//(?:\\\n|[^\n])*|/\*.*?(?:\*/|\Z)",
        re.DOTALL,
    )

    def _repl(match: re.Match[str]) -> str:
        tok = match.group()
        newlines = "\n" * tok.count("\n")
        if tok.startswith(("//", "/*")):
            return " " + newlines
        return '""' + newlines

    return rx.sub(_repl, text)


def extract_expected_check_lines(
    source_path: Path,
    assert_regex: str = DEFAULT_ASSERT_REGEX,
) -> list[CheckLineSpec]:
    """Return all ``CHECK*`` assertion line spans in ``source_path``."""
    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
    stripped = _strip_comments_and_strings_preserve_lines(raw_text)
    raw_lines = raw_text.splitlines()
    stripped_lines = stripped.splitlines()
    rx = re.compile(assert_regex)

    specs: list[CheckLineSpec] = []
    in_pp_directive = False
    for idx, line in enumerate(stripped_lines, start=1):
        raw_line = raw_lines[idx - 1]
        is_continued = raw_line.rstrip().endswith("\\")
        if in_pp_directive:
            in_pp_directive = is_continued
            continue
        if line.lstrip().startswith("#"):
            in_pp_directive = is_continued
            continue
        match = rx.search(line)
        if not match:
            continue
        # Walk parenthesis balance from the matched '(' to find end_line for multiline calls
        paren_pos = line.find("(", match.start())
        depth = 0
        end_line = idx
        if paren_pos >= 0:
            cur_idx = idx
            pos = paren_pos
            while cur_idx <= len(stripped_lines):
                segment = stripped_lines[cur_idx - 1][pos:]
                for ch in segment:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end_line = cur_idx
                            break
                if depth <= 0:
                    break
                cur_idx += 1
                pos = 0
            if depth > 0:
                end_line = idx
        specs.append(CheckLineSpec(start_line=idx, end_line=end_line, text=raw_lines[idx - 1].strip()))
    return specs


def extract_completion_lines(source_path: Path) -> list[int]:
    """Return 1-based line numbers of completion/summary calls in ``source_path``."""
    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
    stripped = _strip_comments_and_strings_preserve_lines(raw_text)
    lines: list[int] = []
    for idx, line in enumerate(stripped.splitlines(), start=1):
        if _COMPLETION_LINE_RE.search(line):
            lines.append(idx)
    return lines


def extract_failure_path_lines(source_path: Path) -> list[int]:
    """Return 1-based line numbers annotated with ``DEVPACK_FAILURE_PATH`` in ``source_path``."""
    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
    lines: list[int] = []
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        if _FAILURE_PATH_RE.search(line):
            lines.append(idx)
    return lines


def extract_success_path_lines(source_path: Path) -> list[int]:
    """Return 1-based line numbers annotated with ``DEVPACK_SUCCESS_PATH`` in ``source_path``."""
    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
    lines: list[int] = []
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        if _SUCCESS_PATH_RE.search(line):
            lines.append(idx)
    return lines


def extract_check_total_lines(source_path: Path) -> list[int]:
    """Return 1-based line numbers tracking total executed checks in ``source_path``."""
    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
    lines: list[int] = []
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        if _CHECK_TOTAL_RE.search(line):
            lines.append(idx)
    return lines


def _find_gcda_and_gcno_for_source(obj_dir: Path, src_path: Path) -> tuple[Path | None, Path | None]:
    """Locate ``.gcno`` and ``.gcda`` files in ``obj_dir`` corresponding to ``src_path``."""
    stem = src_path.stem
    gcno_matches = sorted(obj_dir.rglob(f"{stem}*.gcno"))
    gcda_matches = sorted(obj_dir.rglob(f"{stem}*.gcda"))
    # Prefer exact stem.gcno / stem.cpp.gcno (CMake writes <file>.cpp.gcno)
    exact_gcno = [p for p in gcno_matches if p.name in (f"{stem}.gcno", f"{src_path.name}.gcno")]
    exact_gcda = [p for p in gcda_matches if p.name in (f"{stem}.gcda", f"{src_path.name}.gcda")]
    gcno = exact_gcno[0] if exact_gcno else (gcno_matches[0] if gcno_matches else None)
    gcda = exact_gcda[0] if exact_gcda else (gcda_matches[0] if gcda_matches else None)
    return gcno, gcda


def _parse_gcov_json_payload(payload_text: str) -> dict[str, dict[int, int]]:
    """Parse one or more gcov JSON objects into ``{normalized_file_path: {line_num: count}}``."""
    counts_by_file: dict[str, dict[int, int]] = {}
    decoder = json.JSONDecoder()
    idx = 0
    length = len(payload_text)
    while idx < length:
        while idx < length and payload_text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, end_idx = decoder.raw_decode(payload_text, idx)
        idx = end_idx
        if not isinstance(obj, dict):
            continue
        for file_rec in obj.get("files", []):
            if not isinstance(file_rec, dict):
                continue
            raw_name = str(file_rec.get("file", "")).strip()
            if not raw_name:
                continue
            try:
                norm_key = str(Path(raw_name).resolve())
            except OSError:
                norm_key = raw_name
            line_map = counts_by_file.setdefault(norm_key, {})
            for line_rec in file_rec.get("lines", []):
                if not isinstance(line_rec, dict):
                    continue
                ln = line_rec.get("line_number")
                cnt = line_rec.get("count")
                if isinstance(ln, int) and isinstance(cnt, int):
                    # Sum across template instantiations/functions sharing a line in one .gcno
                    line_map[ln] = line_map.get(ln, 0) + cnt
    return counts_by_file


def _run_gcov_json(gcov_bin: str, gcno_path: Path, obj_dir: Path) -> dict[str, dict[int, int]]:
    """Run ``gcov -j -t`` (or ``gcov -j`` fallback) for ``gcno_path`` and return line execution counts."""
    proc = subprocess.run(
        [gcov_bin, "-j", "-t", str(gcno_path)],
        cwd=str(obj_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return _parse_gcov_json_payload(proc.stdout)

    # Fallback: `gcov -j` writes <name>.gcov.json.gz in cwd
    gz_proc = subprocess.run(
        [gcov_bin, "-j", str(gcno_path)],
        cwd=str(obj_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if gz_proc.returncode != 0:
        raise RuntimeError(
            f"gcov failed on {gcno_path}: {(proc.stderr or gz_proc.stderr or proc.stdout).strip()}"
        )
    merged: dict[str, dict[int, int]] = {}
    for gz_file in sorted(obj_dir.glob("*.gcov.json.gz")):
        text = gzip.decompress(gz_file.read_bytes()).decode("utf-8", errors="replace")
        for fkey, lmap in _parse_gcov_json_payload(text).items():
            dest = merged.setdefault(fkey, {})
            for ln, cnt in lmap.items():
                dest[ln] = dest.get(ln, 0) + cnt
    return merged


def _lookup_line_counts(
    all_counts: dict[str, dict[int, int]],
    src_path: Path,
) -> dict[int, int] | None:
    """Find line count dictionary for ``src_path`` in parsed gcov results."""
    resolved = str(src_path.resolve())
    if resolved in all_counts:
        return all_counts[resolved]
    matches = [lmap for fkey, lmap in all_counts.items() if Path(fkey).name == src_path.name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        merged: dict[int, int] = {}
        for m in matches:
            for ln, cnt in m.items():
                merged[ln] = max(merged.get(ln, 0), cnt)
        return merged
    return None


def clear_gcda_files(obj_dir: Path) -> None:
    """Remove any stale ``.gcda`` files in ``obj_dir`` before executing a test binary."""
    if not obj_dir.is_dir():
        return
    for gcda in obj_dir.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def verify_gcov_proof(
    test_sources: Sequence[Path],
    obj_dir: Path,
    *,
    output_text: str = "",
    returncode: int = 0,
    gcov_bin: str = "gcov",
    assert_regex: str = DEFAULT_ASSERT_REGEX,
    require_nonempty_checks: bool = True,
    label: str = "test_binary",
    min_mtime: float | None = None,
) -> ProofResult:
    """Verify exit 0, no failure lines, fresh .gcda mtime, CHECK execution >=1, and failure-path count == 0."""
    resolved_gcov = find_gcov_bin(gcov_bin)
    if resolved_gcov is None:
        return ProofResult(
            exit_code=3,
            reason=f"TOOL_MISSING: gcov executable '{gcov_bin}' not found on PATH",
            output=output_text,
        )

    if returncode < 0:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} terminated abnormally by signal {-returncode}",
            output=output_text,
        )
    if returncode != 0:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} exited with status {returncode}",
            output=output_text,
        )

    if not test_sources:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} provided no pristine test source files to verify",
            output=output_text,
        )

    all_counts: dict[str, dict[int, int]] = {}
    seen_gcnos: set[Path] = set()
    for src in test_sources:
        if not src.is_file():
            return ProofResult(
                exit_code=1,
                reason=f"FAIL: {label} missing test source {src}",
                output=output_text,
            )
        gcno, gcda = _find_gcda_and_gcno_for_source(obj_dir, src)
        if gcno is None:
            return ProofResult(
                exit_code=1,
                reason=f"FAIL: {label} missing gcov .gcno coverage graph for {src.name}",
                output=output_text,
            )
        if gcda is None:
            return ProofResult(
                exit_code=1,
                reason=f"FAIL: {label} abnormal termination before gcov .gcda data was written for {src.name}",
                output=output_text,
            )
        if min_mtime is not None:
            try:
                gcda_mtime = gcda.stat().st_mtime
            except OSError as exc:
                return ProofResult(
                    exit_code=1,
                    reason=f"FAIL: {label} unable to stat gcov .gcda data for {src.name}: {exc}",
                    output=output_text,
                )
            if gcda_mtime < (min_mtime - _MTIME_JIFFY_SLACK_S):
                return ProofResult(
                    exit_code=1,
                    reason=f"FAIL: {label} stale gcov .gcda data for {src.name} (written before test run started)",
                    output=output_text,
                )
        resolved_gcno = gcno.resolve()
        if resolved_gcno in seen_gcnos:
            continue
        seen_gcnos.add(resolved_gcno)
        try:
            parsed = _run_gcov_json(resolved_gcov, gcno, obj_dir)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            return ProofResult(
                exit_code=1,
                reason=f"FAIL: {label} gcov parsing error for {src.name}: {exc}",
                output=output_text,
            )
        for fkey, lmap in parsed.items():
            dest = all_counts.setdefault(fkey, {})
            for ln, cnt in lmap.items():
                dest[ln] = dest.get(ln, 0) + cnt

    total_verified = 0
    uses_pack_fw = False
    for src in test_sources:
        src_text = src.read_text(encoding="utf-8", errors="replace")
        if _FRAMEWORK_INCLUDE_RE.search(src_text):
            uses_pack_fw = True
        line_counts = _lookup_line_counts(all_counts, src)
        if line_counts is None:
            return ProofResult(
                exit_code=1,
                reason=f"FAIL: {label} gcov output missing coverage entry for {src.name}",
                output=output_text,
            )
        expected_checks = extract_expected_check_lines(src, assert_regex=assert_regex)
        for spec in expected_checks:
            executed = any(
                line_counts.get(ln, 0) >= 1
                for ln in range(spec.start_line, spec.end_line + 1)
            )
            if not executed:
                return ProofResult(
                    exit_code=1,
                    reason=(
                        f"FAIL: {label} unexecuted assertion at {src.name}:{spec.start_line} "
                        f"(gcov count=0): {spec.text}"
                    ),
                    output=output_text,
                )
            total_verified += 1

        for fail_ln in extract_failure_path_lines(src):
            fail_cnt = line_counts.get(fail_ln, 0)
            if fail_cnt > 0:
                return ProofResult(
                    exit_code=1,
                    reason=(
                        f"FAIL: {label} failure-path line {src.name}:{fail_ln} executed "
                        f"{fail_cnt} time(s)"
                    ),
                    output=output_text,
                )

        for comp_ln in extract_completion_lines(src):
            if line_counts.get(comp_ln, 0) < 1:
                return ProofResult(
                    exit_code=1,
                    reason=(
                        f"FAIL: {label} completion line {src.name}:{comp_ln} was never executed "
                        "(early process termination)"
                    ),
                    output=output_text,
                )

    fw_entries = [
        (Path(fkey), lmap)
        for fkey, lmap in all_counts.items()
        if Path(fkey).name == "test_framework.hpp" and Path(fkey).is_file()
    ]
    if uses_pack_fw and not fw_entries and total_verified > 0:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} gcov output missing coverage entry for test_framework.hpp",
            output=output_text,
        )
    for fw_path, fw_counts in fw_entries:
        fw_fail_lines = extract_failure_path_lines(fw_path)
        for fail_ln in fw_fail_lines:
            fail_cnt = fw_counts.get(fail_ln, 0)
            if fail_cnt > 0:
                return ProofResult(
                    exit_code=1,
                    reason=(
                        f"FAIL: {label} framework failure-path line {fw_path.name}:{fail_ln} executed "
                        f"{fail_cnt} time(s)"
                    ),
                    output=output_text,
                )
        if total_verified > 0:
            fw_fail_set = set(fw_fail_lines)
            non_fail_exec = any(cnt >= 1 for ln, cnt in fw_counts.items() if ln not in fw_fail_set)
            if not non_fail_exec:
                return ProofResult(
                    exit_code=1,
                    reason=f"FAIL: {label} test_framework.hpp recorded 0 executed assertions in gcov",
                    output=output_text,
                )
            fw_success_lines = extract_success_path_lines(fw_path)
            if fw_success_lines:
                fw_success_count = sum(fw_counts.get(ln, 0) for ln in fw_success_lines)
                fw_total_lines = extract_check_total_lines(fw_path)
                fw_total_count = sum(fw_counts.get(ln, 0) for ln in fw_total_lines)
                if fw_success_count == 0:
                    return ProofResult(
                        exit_code=1,
                        reason=f"FAIL: {label} {fw_path.name} DEVPACK_SUCCESS_PATH recorded 0 passed checks in gcov",
                        output=output_text,
                    )
                if fw_total_lines and fw_success_count != fw_total_count:
                    return ProofResult(
                        exit_code=1,
                        reason=(
                            f"FAIL: {label} {fw_path.name} success-path count ({fw_success_count}) "
                            f"!= CHECK execution count ({fw_total_count}) in gcov"
                        ),
                        output=output_text,
                    )
                if fw_success_count < total_verified:
                    return ProofResult(
                        exit_code=1,
                        reason=(
                            f"FAIL: {label} {fw_path.name} success-path count ({fw_success_count}) "
                            f"< executed CHECK lines ({total_verified})"
                        ),
                        output=output_text,
                    )
                tally_match = _COMPLETED_TALLY_RE.search(output_text)
                if tally_match:
                    reported_checks = int(tally_match.group(1))
                    reported_failed = int(tally_match.group(2))
                    if reported_failed != 0 or fw_success_count != reported_checks:
                        return ProofResult(
                            exit_code=1,
                            reason=(
                                f"FAIL: {label} {fw_path.name} success-path gcov count ({fw_success_count}) "
                                f"!= DEVPACK_TESTS_COMPLETED checks={reported_checks} failed={reported_failed}"
                            ),
                            output=output_text,
                        )

    if require_nonempty_checks and total_verified == 0:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} found 0 CHECK* lines across pristine test sources",
            output=output_text,
        )

    # Output scan runs last so the gcov proof (the authoritative evidence) names the reason when both apply.
    # Verdict is unchanged: any output with failure lines still fails, even when every gcov check passes.
    if _FAILURE_LINE_RE.search(output_text):
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} printed test failure lines despite exit 0",
            output=output_text,
        )

    return ProofResult(exit_code=0, output=output_text, verified_checks=total_verified)


def run_and_verify_gcov(
    cmd: Sequence[str],
    test_sources: Sequence[Path],
    obj_dir: Path,
    *,
    cwd: Path | None = None,
    gcov_bin: str = "gcov",
    assert_regex: str = DEFAULT_ASSERT_REGEX,
    require_nonempty_checks: bool = True,
    label: str = "test_binary",
    timeout_s: int = 300,
) -> ProofResult:
    """Clear stale ``.gcda`` data, execute ``cmd`` in scrubbed env, and verify gcov proof."""
    resolved_gcov = find_gcov_bin(gcov_bin)
    if resolved_gcov is None:
        return ProofResult(
            exit_code=3,
            reason=f"TOOL_MISSING: gcov executable '{gcov_bin}' not found on PATH",
        )

    clear_gcda_files(obj_dir)
    child_env = dict(os.environ)
    child_env.pop("DEVPACK_HARNESS_NONCE", None)
    child_env.pop("DEVPACK_HARNESS_NONCE_FILE", None)
    start_time = time.time()

    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProofResult(exit_code=1, reason=f"FAIL: {label} timed out after {timeout_s}s")
    except OSError as exc:
        return ProofResult(exit_code=1, reason=f"FAIL: {label} failed to execute: {exc}")

    return verify_gcov_proof(
        test_sources,
        obj_dir,
        output_text=proc.stdout or "",
        returncode=proc.returncode,
        gcov_bin=resolved_gcov,
        assert_regex=assert_regex,
        require_nonempty_checks=require_nonempty_checks,
        label=label,
        min_mtime=start_time,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for ``enforcement/checks/test_proof.py``."""
    parser = argparse.ArgumentParser(
        prog="test_proof.py",
        description="Out-of-process gcov coverage proof runner and verifier for test binaries.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    for name in ("run", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--test-src", action="append", required=True, type=Path, help="Pristine test source file")
        sp.add_argument("--obj-dir", required=True, type=Path, help="Directory holding .gcno/.gcda files")
        sp.add_argument("--gcov-bin", default=os.environ.get("GCOV", "gcov"), help="gcov binary path or name")
        sp.add_argument("--assert-regex", default=DEFAULT_ASSERT_REGEX, help="Regex matching assertion lines")
        sp.add_argument("--allow-zero-checks", action="store_true", help="Allow 0 matched CHECK lines")
        sp.add_argument("--label", default="test_binary", help="Human-readable label for diagnostics")
        if name == "run":
            sp.add_argument("--cwd", type=Path, default=None, help="Working directory for test command")
            sp.add_argument("--timeout-s", type=int, default=300, help="Subprocess timeout in seconds")
            sp.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to execute after '--'")
        else:
            sp.add_argument("--stdout-file", type=Path, default=None, help="Captured output file")
            sp.add_argument("--returncode", type=int, default=0, help="Process return code")
            sp.add_argument("--start-time", type=float, default=None, help="Epoch timestamp before run started")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for out-of-process gcov test proof verification."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.subcommand == "run":
        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("error: 'run' requires a test command after '--'", file=sys.stderr)
            return 2
        result = run_and_verify_gcov(
            cmd,
            args.test_src,
            args.obj_dir,
            cwd=args.cwd,
            gcov_bin=args.gcov_bin,
            assert_regex=args.assert_regex,
            require_nonempty_checks=not args.allow_zero_checks,
            label=args.label,
            timeout_s=args.timeout_s,
        )
    else:
        output_text = ""
        if args.stdout_file is not None and args.stdout_file.is_file():
            output_text = args.stdout_file.read_text(encoding="utf-8", errors="replace")
        result = verify_gcov_proof(
            args.test_src,
            args.obj_dir,
            output_text=output_text,
            returncode=args.returncode,
            gcov_bin=args.gcov_bin,
            assert_regex=args.assert_regex,
            require_nonempty_checks=not args.allow_zero_checks,
            label=args.label,
            min_mtime=args.start_time,
        )

    if result.output:
        target_stream = sys.stderr if result.exit_code != 0 else sys.stdout
        print(result.output, end="" if result.output.endswith("\n") else "\n", file=target_stream)
    if result.exit_code != 0:
        print(result.reason, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
