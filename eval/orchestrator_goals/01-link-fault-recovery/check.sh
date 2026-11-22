#!/usr/bin/env bash
# check.sh - Deterministic grader for orchestrator goal 01-link-fault-recovery.
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Verifies file scope, test lock, new REQ-LINK-006 unit tests, visible fixture
# tests, hidden REQ-LINK-006 behavior tests, and preflight lint.
#
# Exit codes:
#   0 - All grading checks passed
#   1 - One or more grading checks failed
#   2 - Invalid invocation or GRADER_MISCONFIGURED
set -euo pipefail

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

WORKDIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --workdir)
            if [[ $# -lt 2 ]]; then
                echo "error: --workdir requires a directory argument" >&2
                exit 2
            fi
            WORKDIR="$2"
            shift 2
            ;;
        *)
            if [[ -z "$WORKDIR" && -d "$1" ]]; then
                WORKDIR="$1"
                shift
            else
                echo "error: unknown argument '$1' (try --help)" >&2
                exit 2
            fi
            ;;
    esac
done

if [[ -z "${DEVPACK_ROOT:-}" ]]; then
    echo "GRADER_MISCONFIGURED: DEVPACK_ROOT is unset" >&2
    exit 2
fi

PREFLIGHT_LINT="$DEVPACK_ROOT/enforcement/lint/preflight_lint.py"
PREFLIGHT_CFG="$DEVPACK_ROOT/eval/fixture/preflight.toml"
TEST_LOCK="$DEVPACK_ROOT/enforcement/checks/test_lock.py"
SCOPE_CHECK="$DEVPACK_ROOT/enforcement/checks/scope_check.py"

if [[ ! -f "$PREFLIGHT_LINT" || ! -f "$PREFLIGHT_CFG" || ! -f "$TEST_LOCK" || ! -f "$SCOPE_CHECK" ]]; then
    echo "GRADER_MISCONFIGURED: missing enforcement scripts or preflight.toml under DEVPACK_ROOT=$DEVPACK_ROOT" >&2
    exit 2
fi

GOAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIDDEN_SRC="$GOAL_DIR/hidden/test_req_link_006.cpp"
if [[ ! -f "$HIDDEN_SRC" ]]; then
    echo "GRADER_MISCONFIGURED: missing hidden test $HIDDEN_SRC" >&2
    exit 2
fi

if [[ -z "$WORKDIR" ]]; then
    WORKDIR="$PWD"
fi
if [[ ! -d "$WORKDIR" ]]; then
    echo "FAIL: workdir does not exist: $WORKDIR" >&2
    exit 2
fi
WORKDIR="$(cd "$WORKDIR" && pwd)"

if ! git -C "$WORKDIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "FAIL: workdir is not a git repository: $WORKDIR" >&2
    exit 1
fi

BASE_REF="$(git -C "$WORKDIR" log --format=%H --grep='^chore: apply setup\.patch$' -n 1 || true)"
if [[ -z "$BASE_REF" ]]; then
    BASE_REF="$(git -C "$WORKDIR" log --format=%H --grep='^chore: base fixture$' -n 1 || true)"
fi
if [[ -z "$BASE_REF" ]]; then
    BASE_REF="HEAD"
fi

ALLOWED_FILES=(
    "include/link_state_machine.hpp"
    "src/link_state_machine.cpp"
    "tests/test_link_state_machine.cpp"
    "briefs/**"
    "runs/**"
    ".claude/**"
)

python3 "$SCOPE_CHECK" --repo "$WORKDIR" --base "$BASE_REF" --allow "${ALLOWED_FILES[@]}" >/dev/null
python3 "$TEST_LOCK" --repo "$WORKDIR" --base "$BASE_REF" --protected 'tests/**' --no-deviation >/dev/null



# Verify unit tests for REQ-LINK-006 were added to tests/test_link_state_machine.cpp
if ! grep -q 'recoverFromFault' "$WORKDIR/tests/test_link_state_machine.cpp" ||
   ! grep -q 'faultCount' "$WORKDIR/tests/test_link_state_machine.cpp"; then
    echo "FAIL: tests/test_link_state_machine.cpp must add tests for recoverFromFault and faultCount" >&2
    exit 1
fi

# Visible fixture build + tests
bash "$WORKDIR/build_and_test.sh" --no-cmake >/dev/null

# Hidden REQ-LINK-006 test compilation + run
TMP_GRADE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/devpack-grade-goal01.XXXXXX")"
# shellcheck disable=SC2317,SC2329
cleanup() {
    rm -rf "$TMP_GRADE_DIR"
}
trap cleanup EXIT

mapfile -t fixture_srcs < <(find "$WORKDIR/src" -maxdepth 1 -type f -name '*.cpp' | sort)
CXX_BIN="${CXX:-g++}"
CXXFLAGS=(-std=c++17 -Wall -Wextra -Werror -Wpedantic -Wswitch-enum -Wunreachable-code -fno-exceptions -fno-rtti)
"$CXX_BIN" "${CXXFLAGS[@]}" \
    -I"$WORKDIR/include" \
    "${fixture_srcs[@]}" \
    "$HIDDEN_SRC" \
    -o "$TMP_GRADE_DIR/hidden_test"
"$TMP_GRADE_DIR/hidden_test" >/dev/null

# Preflight lint on changed C/C++ files
mapfile -t CHANGED_CPP_FILES < <(
    {
        git -C "$WORKDIR" diff --name-only "$BASE_REF" -- 'include/*.hpp' 'include/*.h' 'src/*.cpp' 'src/*.c'
        git -C "$WORKDIR" ls-files --others --exclude-standard -- 'include/*.hpp' 'include/*.h' 'src/*.cpp' 'src/*.c'
    } | sort -u
)
if [[ "${#CHANGED_CPP_FILES[@]}" -gt 0 ]]; then
    lint_targets=()
    for rel_f in "${CHANGED_CPP_FILES[@]}"; do
        if [[ -f "$WORKDIR/$rel_f" ]]; then
            lint_targets+=("$WORKDIR/$rel_f")
        fi
    done
    if [[ "${#lint_targets[@]}" -gt 0 ]]; then
        python3 "$PREFLIGHT_LINT" --no-cache --repo-root "$WORKDIR" --config "$PREFLIGHT_CFG" "${lint_targets[@]}" >/dev/null
    fi
fi

echo "PASS: 01-link-fault-recovery"
exit 0
