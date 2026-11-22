#!/usr/bin/env bash
# build_and_test.sh - Configure, build, and run unit tests for the C++17 eval fixture.
#
# Usage: bash build_and_test.sh [--help] [--build-dir DIR] [--no-cmake]
#
# Builds the fixture with -Wall -Wextra -Werror -std=c++17 into a temporary build
# directory (or DIR when --build-dir is specified) using CMake if available, or
# falling back to direct g++ compilation. Runs the compiled unit test binary.
#
# Exit codes:
#   0 - Build and all unit tests succeeded
#   1 - Build or unit test failure
#   2 - Invalid command-line arguments
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

if [[ "$USE_CMAKE" -eq 1 ]] && command -v cmake >/dev/null 2>&1; then
    cmake -S "$FIXTURE_ROOT" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD_DIR" --parallel 2 >/dev/null
    "$BUILD_DIR/fixture_tests"
else
    "$CXX_BIN" "${CXXFLAGS[@]}" \
        -I"$FIXTURE_ROOT/include" \
        -I"$FIXTURE_ROOT/tests" \
        "$FIXTURE_ROOT/src/can_codec.cpp" \
        "$FIXTURE_ROOT/src/ring_buffer.cpp" \
        "$FIXTURE_ROOT/src/link_state_machine.cpp" \
        "$FIXTURE_ROOT/src/dtc_debouncer.cpp" \
        "$FIXTURE_ROOT/tests/test_can_codec.cpp" \
        "$FIXTURE_ROOT/tests/test_ring_buffer.cpp" \
        "$FIXTURE_ROOT/tests/test_link_state_machine.cpp" \
        "$FIXTURE_ROOT/tests/test_dtc_debouncer.cpp" \
        "$FIXTURE_ROOT/tests/test_main.cpp" \
        -o "$BUILD_DIR/fixture_tests"
    "$BUILD_DIR/fixture_tests"
fi
