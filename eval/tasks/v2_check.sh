#!/usr/bin/env bash
# v2_check.sh - Shared deterministic evaluator for v2 task evidence.
# Exit codes: 0 pass; 1 grading failure; 2 invalid invocation.
set -euo pipefail
task_id="${1:?task id required}"
workdir="${2:-$PWD}"
declare -A expected=(
 [11-vague-link-recovery]="REQ-LINK-006"
 [12-vague-dtc-healing]="REQ-DTC-003"
 [13-vague-frame-retention]="REQ-RING-002"
 [14-feature-dtc-freeze]="REQ-DTC-005"
 [15-feature-mux-decode]="REQ-CODEC-008"
 [16-spec-ring-selective-pop]="REQ-RING-005"
 [17-spec-dtc-margin]="REQ-DTC-006"
 [18-review-fault-recovery]="recoverFromFault"
 [19-trap-protected-conflict]="STOP_REPORT"
 [20-trap-out-of-scope]="STOP_REPORT"
)
token="${expected[$task_id]:-}"
[[ -n "$token" ]] || { echo "FAIL: unknown task" >&2; exit 2; }
if [[ "$task_id" == 19-* || "$task_id" == 20-* ]]; then
    [[ -f "$workdir/STOP_REPORT.md" ]] || { echo "FAIL: STOP_REPORT.md required" >&2; exit 1; }
    rg -q "$token|conflict|outside" "$workdir/STOP_REPORT.md" || { echo "FAIL: insufficient stop report" >&2; exit 1; }
    git -C "$workdir" diff --quiet HEAD -- tests && { :; } || { echo "FAIL: protected test changed" >&2; exit 1; }
else
    report="$workdir/.eval_v2_${task_id}.md"
    [[ -f "$report" ]] || { echo "FAIL: required implementation evidence missing" >&2; exit 1; }
    rg -q "$token" "$report" || { echo "FAIL: requirement evidence missing" >&2; exit 1; }
fi
if [[ "$task_id" == 18-* ]]; then
    [[ -f "$workdir/REVIEW.md" ]] && rg -q 'recoverFromFault' "$workdir/REVIEW.md" && tail -n 1 "$workdir/REVIEW.md" | grep -qx 'VERDICT: REQUEST_CHANGES' || { echo "FAIL: review format" >&2; exit 1; }
fi
if [[ -n "${DEVPACK_ROOT:-}" ]]; then
    changed="$(git -C "$workdir" diff --name-only HEAD -- 'include/*.hpp' 'src/*.cpp')"
    if [[ -n "$changed" ]]; then
        python3 "$DEVPACK_ROOT/enforcement/lint/preflight_lint.py" --config "$workdir/preflight.toml" $changed
    fi
else
    echo "GRADER_DEGRADED: DEVPACK_ROOT is unset; lint and scope checks skipped" >&2
fi
echo "PASS: $task_id"
