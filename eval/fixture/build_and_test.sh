#!/usr/bin/env bash
# build_and_test.sh - Configure, build, and run unit tests for the C++17 eval fixture.
#
# Usage: bash build_and_test.sh [--help] [--build-dir DIR] [--no-cmake]
#
# Builds the fixture with -Wall -Wextra -Werror -std=c++17 into a temporary build
# directory (or DIR when --build-dir is specified) using CMake if available, or
# falling back to direct g++ compilation with --coverage on TEST translation units
# only and force-included test_framework.hpp. Runs the compiled unit test binary.
#
# Exit codes:
#   0 - Build and all unit tests succeeded
#   1 - Build or unit test failure
#   2 - Invalid command-line arguments
#   3 - Required gcov proof tool missing
set -euo pipefail

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

BUILD_DIR=""
USE_CMAKE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --build-dir)
            if [[ $# -lt 2 ]]; then
                echo "error: --build-dir requires a directory argument" >&2
                exit 2
            fi
            BUILD_DIR="$2"
            shift 2
            ;;
        --no-cmake)
            USE_CMAKE=0
            shift
            ;;
        *)
            echo "error: unknown argument '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done

FIXTURE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_ROOT="${DEVPACK_ROOT:-$(cd "$FIXTURE_ROOT/../.." && pwd)}"
PROOF_PY="$PACK_ROOT/enforcement/checks/test_proof.py"
if [[ ! -f "$PROOF_PY" ]]; then
    echo "TOOL_MISSING: set DEVPACK_ROOT to the trusted pack for gcov proof" >&2
    exit 3
fi
export DEVPACK_ROOT="$PACK_ROOT"
if ! command -v gcov >/dev/null 2>&1; then
    echo "TOOL_MISSING: gcov is required for test proof" >&2
    exit 3
fi
CLEANUP_DIR=""

if [[ -z "$BUILD_DIR" ]]; then
    BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/can-fixture-build.XXXXXX")"
    CLEANUP_DIR="$BUILD_DIR"
else
    mkdir -p "$BUILD_DIR"
fi

cleanup() {
    if [[ -n "$CLEANUP_DIR" && -d "$CLEANUP_DIR" ]]; then
        rm -rf "$CLEANUP_DIR"
    fi
}
trap cleanup EXIT

CXX_BIN="${CXX:-g++}"
CXXFLAGS=(-std=c++17 -Wall -Wextra -Werror -Wpedantic -Wswitch-enum -Wunreachable-code -fno-exceptions -fno-rtti)

COV_OBJ_DIR="${DEVPACK_GCOV_OBJ_DIR:-$BUILD_DIR/cov}"
mkdir -p "$COV_OBJ_DIR"
find "$COV_OBJ_DIR" -name '*.gcda' -delete 2>/dev/null || true

PACK_FRAMEWORK_HDR=""
if [[ -n "${DEVPACK_ROOT:-}" && -f "$DEVPACK_ROOT/eval/fixture/tests/test_framework.hpp" ]]; then
    PACK_FRAMEWORK_HDR="$DEVPACK_ROOT/eval/fixture/tests/test_framework.hpp"
else
    PACK_FRAMEWORK_HDR="$FIXTURE_ROOT/tests/test_framework.hpp"
fi
GUARD_MACRO="DEVPACK_TF_GUARD_$(date +%s)_$$"

mapfile -t SRC_FILES < <(find "$FIXTURE_ROOT/src" -maxdepth 1 -type f -name '*.cpp' | sort)
mapfile -t TEST_FILES < <(find "$FIXTURE_ROOT/tests" -maxdepth 1 -type f -name '*.cpp' | sort)

if [[ "$USE_CMAKE" -eq 1 ]] && command -v cmake >/dev/null 2>&1; then
    find "$BUILD_DIR" -name '*.gcda' -delete
    cmake -S "$FIXTURE_ROOT" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD_DIR" --parallel 2 >/dev/null
    TEST_BIN="$BUILD_DIR/fixture_tests"
    COV_OBJ_DIR="$BUILD_DIR"
else
    SRC_OBJS=()
    for src_f in "${SRC_FILES[@]}"; do
        obj_f="$BUILD_DIR/$(basename "${src_f%.cpp}").o"
        "$CXX_BIN" "${CXXFLAGS[@]}" -I"$FIXTURE_ROOT/include" -c "$src_f" -o "$obj_f"
        SRC_OBJS+=("$obj_f")
    done

    TEST_OBJS=()
    for tst_f in "${TEST_FILES[@]}"; do
        obj_f="$COV_OBJ_DIR/$(basename "${tst_f%.cpp}").o"
        "$CXX_BIN" "${CXXFLAGS[@]}" --coverage \
            "-DDEVPACK_FRAMEWORK_GUARD=$GUARD_MACRO" \
            -include "$PACK_FRAMEWORK_HDR" \
            -I"$FIXTURE_ROOT/include" \
            -I"$FIXTURE_ROOT/tests" \
            -c "$tst_f" -o "$obj_f"
        TEST_OBJS+=("$obj_f")
    done

    TEST_BIN="$BUILD_DIR/fixture_tests"
    "$CXX_BIN" --coverage "${SRC_OBJS[@]}" "${TEST_OBJS[@]}" -o "$TEST_BIN"
fi

TEST_OUT_FILE="$BUILD_DIR/fixture_tests.out"
set +e
env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE "$TEST_BIN" >"$TEST_OUT_FILE" 2>&1
test_rc=$?
set -e
cat "$TEST_OUT_FILE"
if [[ "$test_rc" -ne 0 ]]; then
    exit 1
fi

proof_args=()
for tst_f in "${TEST_FILES[@]}"; do
    proof_args+=(--test-src "$tst_f")
done
python3 "$PROOF_PY" verify \
    "${proof_args[@]}" \
    --obj-dir "$COV_OBJ_DIR" \
    --stdout-file "$TEST_OUT_FILE" \
    --returncode "$test_rc" \
    --label "fixture_tests"
