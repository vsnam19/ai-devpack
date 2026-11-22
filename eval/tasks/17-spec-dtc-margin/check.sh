#!/usr/bin/env bash
# check.sh - Grade task 17-spec-dtc-margin.
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Runs v2_check.sh (scope, test lock, visible + hidden tests, preflight lint)
# and verifies requirement-to-test traceability for REQ-DTC-006 using
# enforcement/checks/trace_check.py.
#
# Exit codes:
#   0 - All grading and traceability checks passed
#   1 - Grading or traceability check failed
#   2 - Invalid arguments or GRADER_MISCONFIGURED
set -euo pipefail

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

TASKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$TASKS_DIR/v2_check.sh" 17-spec-dtc-margin "$@"

WORKDIR=""
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
    arg="${args[$i]}"
    case "$arg" in
        --workdir)
            i=$((i + 1))
            WORKDIR="${args[$i]:-}"
            ;;
        *)
            if [[ -z "$WORKDIR" && -d "$arg" ]]; then
                WORKDIR="$arg"
            fi
            ;;
    esac
    i=$((i + 1))
done
if [[ -z "$WORKDIR" ]]; then
    WORKDIR="$PWD"
fi
WORKDIR="$(cd "$WORKDIR" && pwd)"

TRACE_CHECK="$DEVPACK_ROOT/enforcement/checks/trace_check.py"
if [[ ! -f "$TRACE_CHECK" ]]; then
    echo "GRADER_MISCONFIGURED: missing $TRACE_CHECK" >&2
    exit 2
fi

REQ_FILE="$(mktemp "${TMPDIR:-/tmp}/devpack-req-17.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -f '$REQ_FILE'" EXIT
cp "$WORKDIR/requirements.txt" "$REQ_FILE"
if ! grep -q '^REQ-DTC-006:' "$REQ_FILE"; then
    printf '\nREQ-DTC-006: DtcDebouncer remainingToConfirm shall return remaining distance or 0 when invalid or confirmed.\n' >> "$REQ_FILE"
fi

python3 "$TRACE_CHECK" --repo "$WORKDIR" --requirements "$REQ_FILE" --tests 'tests/*.cpp' >/dev/null
