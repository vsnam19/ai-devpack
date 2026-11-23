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
from typing import Sequence

DEFAULT_ASSERT_REGEX = r"(?<![A-Za-z0-9_])CHECK(?:_EQ|_TRUE|_FALSE|_NEAR)?\s*\("
_FAILURE_LINE_RE = re.compile(
    r"(?m)^(?:\s*FAIL:|.*:\s*CHECK(?:_EQ|_TRUE|_FALSE|_NEAR)?\([^)]*\)\s+failed|.*GRADER_CHECK\s+failed)"
)
_COMPLETION_LINE_RE = re.compile(r"\bprintSummaryAndExitCode\s*(?:<[^>]*>)?\s*\(|DEVPACK_TESTS_COMPLETED")


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
                    line_map[ln] = max(line_map.get(ln, 0), cnt)
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
                dest[ln] = max(dest.get(ln, 0), cnt)
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
) -> ProofResult:
    """Verify exit status 0, absence of failure lines, and gcov execution of all ``CHECK*`` lines."""
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

    if _FAILURE_LINE_RE.search(output_text):
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} printed test failure lines despite exit 0",
            output=output_text,
        )

    if not test_sources:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} provided no pristine test source files to verify",
            output=output_text,
        )

    all_counts: dict[str, dict[int, int]] = {}
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
                dest[ln] = max(dest.get(ln, 0), cnt)

    total_verified = 0
    for src in test_sources:
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

    if require_nonempty_checks and total_verified == 0:
        return ProofResult(
            exit_code=1,
            reason=f"FAIL: {label} found 0 CHECK* lines across pristine test sources",
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
        )

    if result.output:
        target_stream = sys.stderr if result.exit_code != 0 else sys.stdout
        print(result.output, end="" if result.output.endswith("\n") else "\n", file=target_stream)
    if result.exit_code != 0:
        print(result.reason, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
