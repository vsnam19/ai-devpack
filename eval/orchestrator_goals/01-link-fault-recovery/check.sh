#!/usr/bin/env bash
# check.sh - Deterministic grader for orchestrator goal 01-link-fault-recovery.
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Verifies file scope, test lock, preflight lint, and cppcheck BEFORE running
# worker code, then runs visible fixture tests and hidden REQ-LINK-006 tests
# with out-of-process gcov coverage proof.
#
# Exit codes:
#   0 - All grading checks passed
#   1 - One or more grading checks failed
#   2 - Invalid invocation or GRADER_MISCONFIGURED
set -euo pipefail

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
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
    if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
        printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
    fi
    exit 2
fi

PREFLIGHT_LINT="$DEVPACK_ROOT/enforcement/lint/preflight_lint.py"
CPPCHECK_CHECK="$DEVPACK_ROOT/enforcement/static/cppcheck_check.py"
PREFLIGHT_CFG="$DEVPACK_ROOT/eval/fixture/preflight.toml"
TEST_LOCK="$DEVPACK_ROOT/enforcement/checks/test_lock.py"
SCOPE_CHECK="$DEVPACK_ROOT/enforcement/checks/scope_check.py"
SAFE_GIT_SH="$DEVPACK_ROOT/eval/lib/safe_git.sh"

if [[ ! -f "$PREFLIGHT_LINT" || ! -f "$CPPCHECK_CHECK" || ! -f "$PREFLIGHT_CFG" || ! -f "$TEST_LOCK" || ! -f "$SCOPE_CHECK" || ! -f "$SAFE_GIT_SH" ]]; then
    echo "GRADER_MISCONFIGURED: missing enforcement scripts, preflight.toml, or safe_git.sh under DEVPACK_ROOT=$DEVPACK_ROOT" >&2
    if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
        printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
    fi
    exit 2
fi

# shellcheck source=/dev/null
source "$SAFE_GIT_SH"

GOAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HIDDEN_SRC="$GOAL_DIR/hidden/test_req_link_006.cpp"
if [[ ! -f "$HIDDEN_SRC" ]]; then
    grader_misconfigured "missing hidden test $HIDDEN_SRC"
fi

if [[ -z "$WORKDIR" ]]; then
    WORKDIR="$PWD"
fi
if [[ ! -d "$WORKDIR" ]]; then
    echo "FAIL: workdir does not exist: $WORKDIR" >&2
    exit 2
fi
WORKDIR="$(cd "$WORKDIR" && pwd)"

if ! safe_git -C "$WORKDIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "FAIL: workdir is not a git repository: $WORKDIR" >&2
    exit 1
fi

if ! BASE_REF="$(require_baseline_sha "$WORKDIR" "${TRIAL_DIR:-}")"; then
    exit 2
fi

ALLOWED_FILES=(
    "include/link_state_machine.hpp"
    "src/link_state_machine.cpp"
    "tests/test_link_fault_recovery.cpp"
    "briefs/**"
    "runs/**"
    ".claude/**"
)

python3 "$SCOPE_CHECK" --repo "$WORKDIR" --base "$BASE_REF" --allow "${ALLOWED_FILES[@]}" >/dev/null
python3 "$TEST_LOCK" --repo "$WORKDIR" --base "$BASE_REF" --protected 'tests/**' --no-deviation >/dev/null

# Verify unit tests for REQ-LINK-006 were added to the new test file
NEW_TEST_FILE="$WORKDIR/tests/test_link_fault_recovery.cpp"
if [[ ! -f "$NEW_TEST_FILE" ]] ||
   ! grep -q 'recoverFromFault' "$NEW_TEST_FILE" ||
   ! grep -q 'faultCount' "$NEW_TEST_FILE"; then
    echo "FAIL: tests/test_link_fault_recovery.cpp must add tests for recoverFromFault and faultCount" >&2
    exit 1
fi

# Preflight lint and cppcheck BEFORE compiling/executing any worker code
mapfile -t CHANGED_ALL < <(list_changed_files "$WORKDIR" "$BASE_REF")
lint_targets=()
for rel_f in "${CHANGED_ALL[@]}"; do
    case "$rel_f" in
        *.c|*.cc|*.cpp|*.cxx|*.h|*.hh|*.hpp|*.hxx)
            if [[ -f "$WORKDIR/$rel_f" ]]; then
                lint_targets+=("$WORKDIR/$rel_f")
            fi
            ;;
    esac
done
if [[ "${#lint_targets[@]}" -gt 0 ]]; then
    python3 "$PREFLIGHT_LINT" --no-cache --repo-root "$WORKDIR" --config "$PREFLIGHT_CFG" --base "$BASE_REF" "${lint_targets[@]}" >/dev/null
    set +e
    cppcheck_out="$(python3 "$CPPCHECK_CHECK" --repo-root "$WORKDIR" --config "$PREFLIGHT_CFG" "${lint_targets[@]}" 2>&1)"
    cppcheck_rc=$?
    set -e
    if [[ "$cppcheck_rc" -eq 3 ]]; then
        grader_misconfigured "cppcheck TOOL_MISSING: $cppcheck_out"
    elif [[ "$cppcheck_rc" -eq 2 ]]; then
        grader_misconfigured "cppcheck_check execution error: $cppcheck_out"
    elif [[ "$cppcheck_rc" -ne 0 ]]; then
        echo "FAIL: cppcheck static analysis blocking findings:" >&2
        echo "$cppcheck_out" >&2
        exit 1
    fi
fi

# Visible fixture build + tests with gcov out-of-process proof
run_gcov_visible_tests "visible build_and_test" "$WORKDIR" "$NEW_TEST_FILE"

# Hidden REQ-LINK-006 test compilation + run with gcov out-of-process proof
run_gcov_hidden_tests "hidden_test (REQ-LINK-006)" "$WORKDIR" "$GOAL_DIR/hidden" '\bcheckCond\s*\('

echo "PASS: 01-link-fault-recovery"
exit 0
