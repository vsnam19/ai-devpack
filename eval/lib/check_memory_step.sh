#!/usr/bin/env bash
# Header: Trusted memory proposal build gate; invoked through the isolated grader wrapper.
# Usage: check_memory_step.sh --workdir DIR | --help
# Exit codes: 0 lint and gcov tests passed; 1 worker failure; 2 usage/tool error.
set -euo pipefail
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    sed -n '2,4p' "$0" | sed 's/^# //'
    exit 0
fi
if [[ $# -ne 2 || "$1" != --workdir ]]; then
    echo 'Usage: check_memory_step.sh --workdir DIR' >&2
    exit 2
fi
: "${DEVPACK_ROOT:?DEVPACK_ROOT must be set}"
# shellcheck source=/dev/null
source "$DEVPACK_ROOT/eval/lib/safe_git.sh"
workdir="$2"
base_ref="$(require_baseline_sha "$workdir")"
changed="$(list_changed_files "$workdir" "$base_ref")"
mapfile -t changed_files <<< "$changed"
run_static_lint_on_files "$workdir" "${changed_files[@]}"
run_worker_symbol_check "$workdir" "$base_ref"
run_gcov_visible_tests 'memory proposal build' "$workdir"
