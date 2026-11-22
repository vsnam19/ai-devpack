#!/usr/bin/env bash
# verify_tasks.sh - Verify all eval tasks against the C++17 fixture.
#
# Usage: bash eval/tasks/verify_tasks.sh [--help] [--task TASK_ID]
#
# For every task in eval/tasks/<id>/ (skipping only tasks marked Status: rebuilding):
#   1. Copies eval/fixture/ to an isolated temporary directory and initializes git.
#   2. Applies setup.patch and asserts that check.sh FAILS (exits non-zero).
#   3. For v2 non-trap tasks, asserts that STOP-only and marker-only submissions FAIL.
#   4. Applies solution.patch and asserts that check.sh PASSES (exits 0).
#   5. For v2 vague-bugfix, multi-file-feature, and spec-driven tasks:
#      - hidden/ contains >= 1 test source compiled and run by check.sh (verified
#        with a compile-probe against check.sh)
#      - solution.patch changes >= 1 file under src/ or include/
#      - vague-bugfix tasks have a non-empty setup.patch changing src/
#      - check.sh FAILS on solution.patch minus its src/ hunks
#   6. For v2 review tasks, requires review.patch to apply and build, and every
#      negative_*.patch submission to apply and FAIL check.sh. Unknown v2 categories FAIL.
#   7. For category 'trap', requires cheat.patch and asserts check.sh FAILS on it.
#
# Prints a summary table and exits 0 only if every active task passes.
#
# Exit codes:
#   0 - All active tasks verified successfully
#   1 - One or more tasks failed verification
#   2 - Invalid command-line arguments
set -euo pipefail

usage() {
    sed -n '2,/^set -euo pipefail/{ /^set -euo pipefail/d; p; }' "$0" | sed 's/^# \{0,1\}//'
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

printf "%-34s %-18s %-14s %-14s %-8s\n" "TASK" "CATEGORY" "AFTER_SETUP" "AFTER_SOLVE" "RESULT"
printf "%-34s %-18s %-14s %-14s %-8s\n" "----------------------------------" "------------------" "--------------" "--------------" "--------"

TOTAL_TASKS=0
SKIPPED_TASKS=0
FAILED_TASKS=0

for task_dir in "$TASKS_ROOT"/*/; do
    [[ -d "$task_dir" ]] || continue
    task_id="$(basename "$task_dir")"
    if [[ -n "$FILTER_TASK" && "$task_id" != "$FILTER_TASK" ]]; then
        continue
    fi

    TOTAL_TASKS=$((TOTAL_TASKS + 1))
    category="unknown"
    tier="smoke"
    task_status=""
    if [[ -f "$task_dir/task.md" ]]; then
        category="$(awk -F': ' '/^Category:/ {print $2; exit}' "$task_dir/task.md")"
        tier="$(awk -F': ' '/^Tier:/ {print $2; exit}' "$task_dir/task.md")"
        task_status="$(awk -F': ' '/^Status:/ {print $2; exit}' "$task_dir/task.md")"
    fi

    code_change_task=0
    if [[ "$tier" == "v2" ]]; then
        case "$category" in
            vague-bugfix|multi-file-feature|spec-driven) code_change_task=1 ;;
            review|trap) ;;
            *)
                echo "META-CHECK FAIL ($task_id): unknown v2 category '$category'" >&2
                FAILED_TASKS=$((FAILED_TASKS + 1))
                printf "%-34s %-18s %-14s %-14s %-8s\n" "$task_id" "$category" "-" "-" "FAIL"
                continue
                ;;
        esac
    fi

    if [[ "$task_status" == "rebuilding" ]]; then
        SKIPPED_TASKS=$((SKIPPED_TASKS + 1))
        printf "%-34s %-18s %-14s %-14s %-8s\n" "$task_id" "$category" "-" "-" "SKIP"
        continue
    fi

    for req_file in task.md check.sh setup.patch solution.patch; do
        if [[ ! -f "$task_dir/$req_file" ]]; then
            printf "%-34s %-18s %-14s %-14s %-8s\n" "$task_id" "$category" "MISSING_FILE" "-" "FAIL"
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

    stop_only_ok=1
    if [[ "$tier" == "v2" && "$category" != "trap" ]]; then
        printf 'STOP_REPORT\nUnable to proceed.\n' > "$work_dir/STOP_REPORT.md"
        if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
            stop_only_ok=0
        fi
        rm -f "$work_dir/STOP_REPORT.md"

        printf 'REQ-FAKE\n' > "$work_dir/.eval_v2_${task_id}.md"
        if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
            stop_only_ok=0
        fi
        rm -f "$work_dir/.eval_v2_${task_id}.md"
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

    # Structural meta-checks for v2 code-change tasks
    v2_meta_ok=1
    if [[ "$code_change_task" -eq 1 ]]; then
        # 1) hidden/ contains >= 1 test source compiled and run by check.sh
        mapfile -t hidden_srcs < <(find "$task_dir/hidden" -maxdepth 1 -type f \( -name '*.cpp' -o -name '*.c' -o -name '*.cc' \) 2>/dev/null | sort || true)
        if [[ "${#hidden_srcs[@]}" -eq 0 ]]; then
            echo "META-CHECK FAIL ($task_id): hidden/ has no test source" >&2
            v2_meta_ok=0
        else
            probe_tasks_dir="$TMP_ROOT/probe_${task_id}"
            mkdir -p "$probe_tasks_dir"
            cp "$TASKS_ROOT/v2_check.sh" "$probe_tasks_dir/v2_check.sh"
            cp -r "$task_dir" "$probe_tasks_dir/$task_id"
            for hsrc in "$probe_tasks_dir/$task_id/hidden"/*; do
                [[ -f "$hsrc" ]] || continue
                printf '\n#error "meta-check probe: hidden test must be compiled"\n' >> "$hsrc"
            done
            if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$probe_tasks_dir/$task_id/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
                echo "META-CHECK FAIL ($task_id): check.sh did not compile and run hidden/ test source" >&2
                v2_meta_ok=0
            fi
        fi

        # 2) solution.patch changes >= 1 file under src/ or include/
        if ! grep -qE '^diff --git a/(src|include)/' "$task_dir/solution.patch"; then
            echo "META-CHECK FAIL ($task_id): solution.patch does not change any file under src/ or include/" >&2
            v2_meta_ok=0
        fi

        # 3) vague-bugfix tasks have a non-empty setup.patch that changes src/
        if [[ "$category" == "vague-bugfix" ]]; then
            if [[ ! -s "$task_dir/setup.patch" ]] || ! grep -qE '^diff --git a/src/' "$task_dir/setup.patch"; then
                echo "META-CHECK FAIL ($task_id): vague-bugfix setup.patch must be non-empty and change src/" >&2
                v2_meta_ok=0
            fi
        fi

        # 4) check.sh FAILS on solution.patch minus its src/ hunks
        no_src_dir="$TMP_ROOT/nosrc_${task_id}"
        mkdir -p "$no_src_dir"
        cp -r "$work_dir/." "$no_src_dir/"
        (
            cd "$no_src_dir"
            git reset -q --hard HEAD
            git clean -q -fd
        )
        no_src_patch="$TMP_ROOT/nosrc_${task_id}.patch"
        python3 - "$task_dir/solution.patch" "$no_src_patch" <<'PY'
import pathlib
import sys

patch_text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
kept_sections = []
current = []
skip = False

for line in patch_text.splitlines(keepends=True):
    if line.startswith("diff --git "):
        if current and not skip:
            kept_sections.append("".join(current))
        current = [line]
        parts = line.split()
        a_path = parts[2] if len(parts) > 2 else ""
        skip = a_path.startswith("a/src/")
    else:
        current.append(line)

if current and not skip:
    kept_sections.append("".join(current))

pathlib.Path(sys.argv[2]).write_text("".join(kept_sections), encoding="utf-8")
PY
        if [[ -s "$no_src_patch" ]]; then
            git -C "$no_src_dir" apply "$no_src_patch"
        fi
        if (cd "$no_src_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$no_src_dir" >/dev/null 2>&1); then
            echo "META-CHECK FAIL ($task_id): check.sh passed on solution.patch minus src/ hunks" >&2
            v2_meta_ok=0
        fi
    fi

    # Review tasks grade a document about a buildable seeded patch.
    if [[ "$tier" == "v2" && "$category" == "review" ]]; then
        review_dir="$TMP_ROOT/review_${task_id}"
        mkdir -p "$review_dir"
        cp -r "$work_dir/." "$review_dir/"
        git -C "$review_dir" reset -q --hard HEAD
        git -C "$review_dir" clean -q -fd
        if [[ ! -f "$task_dir/review.patch" ]] ||
            ! git -C "$review_dir" apply "$task_dir/review.patch" ||
            ! bash "$review_dir/build_and_test.sh" --no-cmake >/dev/null 2>&1; then
            echo "META-CHECK FAIL ($task_id): review.patch must apply and pass fixture build" >&2
            v2_meta_ok=0
        fi

        negative_count=0
        for negative_patch in "$task_dir"/negative_*.patch; do
            [[ -f "$negative_patch" ]] || continue
            negative_count=$((negative_count + 1))
            git -C "$review_dir" reset -q --hard HEAD
            git -C "$review_dir" clean -q -fd
            if ! git -C "$review_dir" apply "$negative_patch"; then
                echo "META-CHECK FAIL ($task_id): $(basename "$negative_patch") does not apply" >&2
                v2_meta_ok=0
            elif (cd "$review_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$review_dir" >/dev/null 2>&1); then
                echo "META-CHECK FAIL ($task_id): check.sh accepted $(basename "$negative_patch")" >&2
                v2_meta_ok=0
            fi
        done
        if [[ "$negative_count" -eq 0 ]]; then
            echo "META-CHECK FAIL ($task_id): review task has no negative_*.patch submissions" >&2
            v2_meta_ok=0
        fi
    fi

    # Trap tasks require a cheat.patch which must be rejected by check.sh.
    trap_cheat_ok=1
    if [[ "$category" == "trap" ]]; then
        if [[ ! -f "$task_dir/cheat.patch" ]]; then
            echo "META-CHECK FAIL ($task_id): trap task missing cheat.patch" >&2
            trap_cheat_ok=0
        else
            git -C "$work_dir" apply -R "$task_dir/solution.patch"
            git -C "$work_dir" apply "$task_dir/cheat.patch"
            if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" bash "$task_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
                echo "META-CHECK FAIL ($task_id): check.sh accepted cheat.patch" >&2
                trap_cheat_ok=0
            fi
        fi
    fi

    if [[ "$after_setup" == "FAIL(OK)" && "$after_solve" == "PASS(OK)" && "$stop_only_ok" -eq 1 && "$v2_meta_ok" -eq 1 && "$trap_cheat_ok" -eq 1 ]]; then
        result="OK"
    else
        result="MISMATCH"
        FAILED_TASKS=$((FAILED_TASKS + 1))
    fi

    printf "%-34s %-18s %-14s %-14s %-8s\n" "$task_id" "$category" "$after_setup" "$after_solve" "$result"
done

echo ""
echo "Verified $TOTAL_TASKS task(s) ($SKIPPED_TASKS skipped), $FAILED_TASKS mismatch(es)."

if [[ "$TOTAL_TASKS" -eq 0 || "$FAILED_TASKS" -gt 0 ]]; then
    exit 1
fi
exit 0
