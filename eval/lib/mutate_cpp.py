#!/usr/bin/env python3
# mutate_cpp.py - Run an isolated worker C++ test file against source mutants.
#
# Usage:
#   python3 mutate_cpp.py --workdir DIR --test-file REL_PATH --spec-stdin
#   python3 mutate_cpp.py --help
#
# Compiles all src/*.cpp once into a temporary directory, compiles the worker's
# standalone test file (force-including the pack's test_framework.hpp) and a
# harness-owned main() calling runRegisteredTestSuites(), verifies the
# unmutated binary passes, and then for each mutant in the JSON specification
# recompiles only the mutated source file, links, and asserts the binary exits
# non-zero (mutant killed).
#
# Exit codes:
#   0 - All mutants were killed by the worker's test file
#   1 - Candidate failure: worker test failed on unmutated code, did not kill a mutant,
#       or a source/mutant file does not compile or link against the candidate
#       (MUTANT-BUILD-FAILED). Everything caused by candidate code is exit 1.
#   2 - Harness precondition failure before any candidate file was compiled (pack framework
#       missing, invalid mutant spec, usage). Exit 2 is honoured as a harness error only when
#       a valid harness_error.json marker is also written (see write_harness_marker).
"""Isolated C++ mutation runner for worker-authored test files."""

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
from typing import Any


MUTANT_MAIN_CPP = """#include "test_framework.hpp"

int main() {
    can_fixture_test::runRegisteredTestSuites();
    return can_fixture_test::printSummaryAndExitCode<0>();
}
"""


def write_harness_marker(stage: str, reason: str) -> bool:
    """Write the structured pre-candidate harness marker if the wrapper provided a dir and nonce.

    The wrapper reads ``harness_error.json`` from ``DEVPACK_HARNESS_MARKER_DIR`` and accepts it
    only when it echoes ``DEVPACK_HARNESS_MARKER_NONCE``. Returns True when the marker was written.
    """
    marker_dir = os.environ.get("DEVPACK_HARNESS_MARKER_DIR", "")
    nonce = os.environ.get("DEVPACK_HARNESS_MARKER_NONCE", "")
    if not marker_dir or not nonce:
        return False
    payload = {"nonce": nonce, "stage": stage, "reason": reason}
    try:
        (Path(marker_dir) / "harness_error.json").write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return False
    return True


def mask_cpp_comments_and_literals(text: str) -> str:
    """Replace C/C++ comments and string/char literals with spaces, preserving lengths and newlines."""
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i + 1 < n:
                out[i] = " "
                out[i + 1] = " "
                i += 2
            else:
                i = n
            continue
        if ch in ('"', "'"):
            quote = ch
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\":
                    if i < n and text[i] != "\n":
                        out[i] = " "
                    if i + 1 < n and text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def replace_function_body(text: str, func_regex: str, new_body: str) -> str:
    """Replace the brace-enclosed body of the C++ function matched by ``func_regex``."""
    masked = mask_cpp_comments_and_literals(text)
    pattern = re.compile(func_regex, re.MULTILINE)
    match = pattern.search(masked)
    if not match:
        raise ValueError(f"function pattern not found: {func_regex}")

    idx = match.end()
    # Walk from match end to find opening '{' of the function body (abort if ';' appears first).
    while idx < len(masked):
        ch = masked[idx]
        if ch == "{":
            break
        if ch == ";":
            raise ValueError(f"matched declaration instead of definition for: {func_regex}")
        idx += 1
    if idx >= len(masked) or masked[idx] != "{":
        raise ValueError(f"opening brace not found for: {func_regex}")

    open_idx = idx
    depth = 0
    while idx < len(masked):
        if masked[idx] == "{":
            depth += 1
        elif masked[idx] == "}":
            depth -= 1
            if depth == 0:
                return text[:open_idx] + new_body + text[idx + 1 :]
        idx += 1
    raise ValueError(f"unterminated function body for: {func_regex}")


def apply_mutant(text: str, mutant: dict[str, Any]) -> str:
    """Apply one mutant specification dict to source text and return the mutated text."""
    mode = mutant.get("mode", "replace")
    if mode == "replace":
        old = mutant["old"]
        new = mutant["new"]
        if old not in text:
            raise ValueError(f"target snippet not found for mutant {mutant.get('name', '?')}")
        mutated = text.replace(old, new, 1)
        if mutated == text:
            raise ValueError(f"no-op replacement for mutant {mutant.get('name', '?')}")
        return mutated
    if mode == "replace_body":
        return replace_function_body(text, mutant["func_regex"], mutant["body"])
    raise ValueError(f"unknown mutant mode: {mode}")


def run_mutants(workdir: Path, test_file: str, mutants: list[dict[str, Any]], pack_root: Path) -> int:
    """Compile baseline + worker test and verify every mutant compiles and fails execution."""
    test_path = workdir / test_file
    if not test_path.is_file():
        print(f"FAIL: missing new test file {test_file}", file=sys.stderr)
        return 1

    pack_fw = pack_root / "eval" / "fixture" / "tests" / "test_framework.hpp"
    pack_tests_dir = pack_root / "eval" / "fixture" / "tests"
    if not pack_fw.is_file():
        reason = f"missing pack framework {pack_fw}"
        write_harness_marker("pack_file", reason)
        print(f"GRADER_MISCONFIGURED: {reason}", file=sys.stderr)
        return 2

    # Candidate compile and run steps below must not inherit the harness marker nonce or directory.
    for marker_var in ("DEVPACK_HARNESS_MARKER_DIR", "DEVPACK_HARNESS_MARKER_NONCE"):
        os.environ.pop(marker_var, None)

    src_dir = workdir / "src"
    inc_dir = workdir / "include"
    src_files = sorted(src_dir.glob("*.cpp"))
    if not src_files:
        # The candidate owns src/; removing every source file is a candidate failure.
        print(f"FAIL: no .cpp files under {src_dir} (MUTANT-BUILD-FAILED)", file=sys.stderr)
        return 1

    cxx = os.environ.get("CXX", "g++")
    cxxflags = [
        "-std=c++17",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wpedantic",
        "-Wswitch-enum",
        "-Wunreachable-code",
        "-fno-exceptions",
        "-fno-rtti",
    ]
    guard_macro = f"DEVPACK_TF_GUARD_{int(time.time())}_{os.getpid()}"

    with tempfile.TemporaryDirectory(prefix="devpack-mutant-") as tmp:
        tmp_path = Path(tmp)
        src_objs: dict[str, Path] = {}
        for sf in src_files:
            obj = tmp_path / f"{sf.stem}.o"
            res = subprocess.run(
                [cxx, *cxxflags, f"-I{inc_dir}", "-c", str(sf), "-o", str(obj)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                print(f"FAIL: failed to compile {sf.name}:\n{res.stderr}", file=sys.stderr)
                return 1
            src_objs[f"src/{sf.name}"] = obj

        main_cpp = tmp_path / "mutant_main.cpp"
        main_cpp.write_text(MUTANT_MAIN_CPP, encoding="utf-8")
        main_obj = tmp_path / "mutant_main.o"
        test_obj = tmp_path / "worker_test.o"
        fw_flags = [
            f"-DDEVPACK_FRAMEWORK_GUARD={guard_macro}",
            "-include",
            str(pack_fw),
            f"-I{inc_dir}",
            f"-I{pack_tests_dir}",
        ]
        for label, cpp_in, obj_out in (
            ("mutant_main.cpp", main_cpp, main_obj),
            (test_file, test_path, test_obj),
        ):
            res = subprocess.run(
                [cxx, *cxxflags, *fw_flags, "-c", str(cpp_in), "-o", str(obj_out)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                print(f"FAIL: failed to compile {label}:\n{res.stderr}", file=sys.stderr)
                return 1

        # Verify the worker's isolated test file passes on unmutated code.
        base_bin = tmp_path / "baseline_runner"
        link_base = subprocess.run(
            [cxx, *[str(p) for p in src_objs.values()], str(test_obj), str(main_obj), "-o", str(base_bin)],
            capture_output=True,
            text=True,
            check=False,
        )
        if link_base.returncode != 0:
            print(f"FAIL: failed to link isolated worker test {test_file}:\n{link_base.stderr}", file=sys.stderr)
            return 1
        run_base = subprocess.run([str(base_bin)], capture_output=True, text=True, check=False)
        if run_base.returncode != 0:
            print(
                f"FAIL: isolated worker test {test_file} failed on unmutated code:\n{run_base.stdout}\n{run_base.stderr}",
                file=sys.stderr,
            )
            return 1

        # Run each mutant against the worker's isolated test file.
        for idx, mutant in enumerate(mutants, 1):
            name = str(mutant.get("name", f"mutant_{idx}"))
            rel_file = str(mutant["file"])
            target_src = workdir / rel_file
            if not target_src.is_file() or rel_file not in src_objs:
                print(f"FAIL: mutant target {rel_file} is missing from the candidate (MUTANT-TARGET-MISSING)",
                      file=sys.stderr)
                return 1
            try:
                mutated_text = apply_mutant(target_src.read_text(encoding="utf-8"), mutant)
            except (KeyError, ValueError) as exc:
                print(f"FAIL: mutant '{name}' cannot be applied to the candidate (MUTANT-APPLY-FAILED): {exc}",
                      file=sys.stderr)
                return 1

            mut_src = tmp_path / f"mut_{idx}_{target_src.name}"
            mut_src.write_text(mutated_text, encoding="utf-8")
            mut_obj = tmp_path / f"mut_{idx}.o"
            comp = subprocess.run(
                [cxx, *cxxflags, f"-I{inc_dir}", "-c", str(mut_src), "-o", str(mut_obj)],
                capture_output=True,
                text=True,
                check=False,
            )
            if comp.returncode != 0:
                # The mutant is built from the candidate's own source, so a compile failure
                # is a candidate failure (MUTANT-BUILD-FAILED), not a harness error.
                print(
                    f"FAIL: MUTANT-BUILD-FAILED mutant '{name}' does not compile against the candidate:\n{comp.stderr}",
                    file=sys.stderr,
                )
                return 1

            mut_objs = [mut_obj if k == rel_file else obj for k, obj in src_objs.items()]
            mut_bin = tmp_path / f"mut_{idx}_runner"
            link = subprocess.run(
                [cxx, *[str(p) for p in mut_objs], str(test_obj), str(main_obj), "-o", str(mut_bin)],
                capture_output=True,
                text=True,
                check=False,
            )
            if link.returncode != 0:
                print(
                    f"FAIL: MUTANT-BUILD-FAILED mutant '{name}' does not link against the candidate:\n{link.stderr}",
                    file=sys.stderr,
                )
                return 1

            run_mut = subprocess.run([str(mut_bin)], capture_output=True, text=True, check=False)
            if run_mut.returncode == 0:
                print(f"FAIL: {test_file} did not catch mutant {idx} ({name})", file=sys.stderr)
                return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run isolated mutation checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--test-file", required=True)
    parser.add_argument("--spec-file", type=Path)
    parser.add_argument("--spec-stdin", action="store_true")
    args = parser.parse_args(argv)

    if bool(args.spec_file) == bool(args.spec_stdin):
        parser.error("specify exactly one of --spec-file or --spec-stdin")

    raw_spec = args.spec_file.read_text(encoding="utf-8") if args.spec_file else sys.stdin.read()
    try:
        mutants = json.loads(raw_spec)
    except ValueError as exc:
        write_harness_marker("mutant_spec", f"mutant spec is not valid JSON: {exc}")
        print(f"GRADER_MISCONFIGURED: mutant spec is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(mutants, list) or not mutants:
        write_harness_marker("mutant_spec", "mutant spec must be a non-empty JSON list")
        print("GRADER_MISCONFIGURED: mutant spec must be a non-empty JSON list", file=sys.stderr)
        return 2

    pack_root_env = os.environ.get("DEVPACK_ROOT")
    pack_root = Path(pack_root_env).resolve() if pack_root_env else Path(__file__).resolve().parents[2]
    return run_mutants(args.workdir.resolve(), args.test_file, mutants, pack_root)


if __name__ == "__main__":
    raise SystemExit(main())
