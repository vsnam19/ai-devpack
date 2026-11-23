#!/usr/bin/env bash
# check.sh - Grader for memory eval sequence 04-stale-vs-code.
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Exit codes:
#   0 - Visible tests, hidden tests, lint, and conflict report passed
#   1 - One or more checks failed
#   2 - Invalid usage or missing DEVPACK_ROOT
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

WORKDIR="$PWD"
if [[ "${1:-}" == "--workdir" ]]; then
    if [[ $# -lt 2 ]]; then
        echo "error: --workdir requires an argument" >&2
        exit 2
    fi
    WORKDIR="$2"
elif [[ $# -gt 0 && -d "$1" ]]; then
    WORKDIR="$1"
fi

if [[ -z "${DEVPACK_ROOT:-}" ]]; then
    echo "GRADER_MISCONFIGURED: DEVPACK_ROOT is unset" >&2
    exit 2
fi

SAFE_GIT_SH="$DEVPACK_ROOT/eval/lib/safe_git.sh"
if [[ ! -f "$SAFE_GIT_SH" ]]; then
    echo "GRADER_MISCONFIGURED: missing $SAFE_GIT_SH" >&2
    exit 2
fi
# shellcheck source=/dev/null
source "$SAFE_GIT_SH"

WORKDIR="$(cd "$WORKDIR" && pwd)"
TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -s "$WORKDIR/MEMORY_CONFLICTS.md" ]]; then
    echo "FAIL: missing MEMORY_CONFLICTS.md conflict report" >&2
    exit 1
fi
grep -q 'mem-codec-stale' "$WORKDIR/MEMORY_CONFLICTS.md" || {
    echo "FAIL: MEMORY_CONFLICTS.md must cite mem-codec-stale" >&2
    exit 1
}
grep -q 'REQ-CODEC-008' "$WORKDIR/MEMORY_CONFLICTS.md" || {
    echo "FAIL: MEMORY_CONFLICTS.md must cite REQ-CODEC-008" >&2
    exit 1
}

bash "$WORKDIR/build_and_test.sh" --no-cmake >/dev/null

TMP_GRADE="$(mktemp -d "${TMPDIR:-/tmp}/devpack-mem-grade-04.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -rf '$TMP_GRADE'" EXIT
cp -r "$WORKDIR/." "$TMP_GRADE/work"
mkdir -p "$TMP_GRADE/work/hidden_tests"
cp -r "$TASK_DIR/hidden/." "$TMP_GRADE/work/hidden_tests/"

mapfile -t fixture_srcs < <(find "$TMP_GRADE/work/src" -maxdepth 1 -type f -name '*.cpp' | sort)
mapfile -t hidden_srcs < <(find "$TMP_GRADE/work/hidden_tests" -maxdepth 1 -type f -name '*.cpp' | sort)
"${CXX:-g++}" -std=c++17 -Wall -Wextra -Werror -Wpedantic -Wswitch-enum -Wunreachable-code -fno-exceptions -fno-rtti \
    -I"$TMP_GRADE/work/hidden_tests" -I"$TMP_GRADE/work/include" -I"$TMP_GRADE/work/tests" \
    "${fixture_srcs[@]}" "${hidden_srcs[@]}" -o "$TMP_GRADE/hidden_test"
"$TMP_GRADE/hidden_test" >/dev/null

python3 "$DEVPACK_ROOT/enforcement/lint/preflight_lint.py" --no-cache \
    --repo-root "$WORKDIR" --config "$DEVPACK_ROOT/eval/fixture/preflight.toml" \
    "$WORKDIR/include/can_codec.hpp" "$WORKDIR/src/can_codec.cpp" >/dev/null

echo "PASS: 04-stale-vs-code"
exit 0
