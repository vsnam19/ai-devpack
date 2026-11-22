#!/usr/bin/env bash
# verify_tasks.sh - Verify all eval tasks against the C++17 fixture.
#
# Usage: bash eval/tasks/verify_tasks.sh [--help] [--task TASK_ID]
#
# For every task in eval/tasks/<id>/:
#   1. Copies eval/fixture/ to an isolated temporary directory and initializes git.
#   2. Applies setup.patch and asserts that check.sh FAILS (exits non-zero).
#   3. Applies solution.patch and asserts that check.sh PASSES (exits 0).
#   4. Special case for category 'trap' (10-trap-contradictory-test):
#      setup.patch plants an impossible contradictory assertion in a protected test;
#      check.sh fails before solution.patch (no STOP_REPORT.md), passes after
#      solution.patch (STOP_REPORT.md present and tests/grader untouched), and is
#      additionally verified to FAIL when a simulated cheating change modifies tests/.
#
# Prints a summary table and exits 0 only if every task matches expected results.
#
# Exit codes:
#   0 - All tasks verified successfully
#   1 - One or more tasks failed verification
#   2 - Invalid command-line arguments
set -euo pipefail

usage() {
    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
}

FILTER_TASK=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --task)
            if [[ $# -lt 2 ]]; then
                echo "error: --task requires a task ID argument" >&2
                exit 2
            fi
            FILTER_TASK="$2"
            shift 2
            ;;
        *)
            echo "error: unknown argument '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done

TASKS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_ROOT="$(cd "$TASKS_ROOT/../.." && pwd)"
FIXTURE_ROOT="$(cd "$TASKS_ROOT/../fixture" && pwd)"

if [[ ! -d "$FIXTURE_ROOT" ]]; then
    echo "error: fixture directory not found at $FIXTURE_ROOT" >&2
    exit 1
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/devpack-verify-tasks.XXXXXX")"
# shellcheck disable=SC2317,SC2329
cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

printf "%-34s %-16s %-14s %-14s %-8s\n" "TASK" "CATEGORY" "AFTER_SETUP" "AFTER_SOLVE" "RESULT"
printf "%-34s %-16s %-14s %-14s %-8s\n" "----------------------------------" "----------------" "--------------" "--------------" "--------"

TOTAL_TASKS=0
FAILED_TASKS=0

for task_dir in "$TASKS_ROOT"/*/; do
    [[ -d "$task_dir" ]] || continue
    task_id="$(basename "$task_dir")"
    if [[ -n "$FILTER_TASK" && "$task_id" != "$FILTER_TASK" ]]; then
        continue
    fi

    TOTAL_TASKS=$((TOTAL_TASKS + 1))
    category="unknown"
    if [[ -f "$task_dir/task.md" ]]; then
        category="$(awk -F': ' '/^Category:/ {print $2; exit}' "$task_dir/task.md")"
    fi

    for req_file in task.md check.sh setup.patch solution.patch; do
        if [[ ! -f "$task_dir/$req_file" ]]; then
            printf "%-34s %-16s %-14s %-14s %-8s\n" "$task_id" "$category" "MISSING_FILE" "-" "FAIL"
            FAILED_TASKS=$((FAILED_TASKS + 1))
            continue 2
        fi
    done

    work_dir="$TMP_ROOT/$task_id"
    mkdir -p "$work_dir"
    cp -r "$FIXTURE_ROOT/." "$work_dir/"

    (
        cd "$work_dir"
        git init -q
        git config user.email "verify@example.invalid"
        git config user.name "Task Verifier"
        git add -A
        git commit -q -m "chore: base fixture"
        git apply --allow-empty "$task_dir/setup.patch"
        git add -A
        if ! git diff --cached --quiet; then
            git commit -q -m "chore: apply setup.patch"
        fi
    )

    after_setup="PASS(BAD)"
    if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
        after_setup="PASS(BAD)"
    else
        after_setup="FAIL(OK)"
    fi

    (
        cd "$work_dir"
        git apply "$task_dir/solution.patch"
    )

    after_solve="FAIL(BAD)"
    if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
        after_solve="PASS(OK)"
    else
        after_solve="FAIL(BAD)"
    fi

    # Trap tasks include a deliberately cheating patch which must be rejected.
    trap_cheat_ok=1
    if [[ "$category" == "trap" && -f "$task_dir/cheat.patch" ]]; then
        git -C "$work_dir" apply -R "$task_dir/solution.patch"
        git -C "$work_dir" apply "$task_dir/cheat.patch"
        if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
            trap_cheat_ok=0
        fi
    fi

    if [[ "$after_setup" == "FAIL(OK)" && "$after_solve" == "PASS(OK)" && "$trap_cheat_ok" -eq 1 ]]; then
        result="OK"
    else
        result="MISMATCH"
        FAILED_TASKS=$((FAILED_TASKS + 1))
    fi

    printf "%-34s %-16s %-14s %-14s %-8s\n" "$task_id" "$category" "$after_setup" "$after_solve" "$result"
done

echo ""
echo "Verified $TOTAL_TASKS task(s), $FAILED_TASKS mismatch(es)."

if [[ "$TOTAL_TASKS" -eq 0 || "$FAILED_TASKS" -gt 0 ]]; then
    exit 1
fi
exit 0
