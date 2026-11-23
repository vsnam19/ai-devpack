#!/usr/bin/env bash
# check.sh - Grade task 18-review-fault-recovery.
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Verifies scope, test lock, absence of marker/STOP files, that review.patch
# applies and compiles cleanly against the fixture, and that REVIEW.md contains
# a review-taxonomy table with a blocker/major functional finding naming
# recoverFromFault and src/link_state_machine.cpp:131 (+-3 lines) and ending
# with VERDICT: REQUEST_CHANGES.
#
# Exit codes:
#   0 - All review grading checks passed
#   1 - One or more review grading checks failed
#   2 - Invalid arguments or GRADER_MISCONFIGURED
set -euo pipefail

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

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

TEST_LOCK="$DEVPACK_ROOT/enforcement/checks/test_lock.py"
SCOPE_CHECK="$DEVPACK_ROOT/enforcement/checks/scope_check.py"
SAFE_GIT_SH="$DEVPACK_ROOT/eval/lib/safe_git.sh"
if [[ ! -f "$TEST_LOCK" || ! -f "$SCOPE_CHECK" || ! -f "$SAFE_GIT_SH" ]]; then
    echo "GRADER_MISCONFIGURED: missing enforcement scripts or safe_git.sh under DEVPACK_ROOT=$DEVPACK_ROOT" >&2
    if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
        printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
    fi
    exit 2
fi

# shellcheck source=/dev/null
source "$SAFE_GIT_SH"

TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVIEW_PATCH="$TASK_DIR/review.patch"
if [[ ! -f "$REVIEW_PATCH" ]]; then
    grader_misconfigured "missing $REVIEW_PATCH"
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

if find "$WORKDIR" -name '.eval_v2_*' -print -quit | grep -q .; then
    echo "FAIL: forbidden .eval_v2_* marker file found in workdir" >&2
    exit 1
fi

if [[ -f "$WORKDIR/STOP_REPORT.md" ]]; then
    echo "FAIL: STOP_REPORT.md is not accepted for review task 18-review-fault-recovery" >&2
    exit 1
fi

if ! BASE_REF="$(require_baseline_sha "$WORKDIR" "${TRIAL_DIR:-}")"; then
    exit 2
fi

python3 "$SCOPE_CHECK" --repo "$WORKDIR" --base "$BASE_REF" --allow 'REVIEW.md' 'STOP_REPORT.md' >/dev/null
python3 "$TEST_LOCK" --repo "$WORKDIR" --base "$BASE_REF" --protected 'tests/**' --no-deviation >/dev/null


# Verify review.patch applies and compiles cleanly against the fixture in workdir.
TMP_PATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/devpack-review-18.XXXXXX")"
# shellcheck disable=SC2064
trap "rm -rf '$TMP_PATCH_DIR'" EXIT
cp -r "$WORKDIR/." "$TMP_PATCH_DIR/work/"
if ! safe_git -C "$TMP_PATCH_DIR/work" apply "$REVIEW_PATCH"; then
    echo "FAIL: review.patch failed to apply to fixture" >&2
    exit 1
fi
if ! run_gcov_visible_tests "review.patch build_and_test" "$TMP_PATCH_DIR/work"; then
    echo "FAIL: review.patch failed to compile and pass fixture build" >&2
    exit 1
fi

REVIEW_FILE="$WORKDIR/REVIEW.md"
if [[ ! -s "$REVIEW_FILE" ]]; then
    echo "FAIL: REVIEW.md is missing or empty" >&2
    exit 1
fi

# Validate taxonomy table, defect function & file:line (+-3 lines of 131), and final VERDICT line.
python3 - "$REVIEW_FILE" <<'PY'
import pathlib
import re
import sys

review_path = pathlib.Path(sys.argv[1])
text = review_path.read_text(encoding="utf-8")
lines = text.splitlines()

if not lines or lines[-1].strip() != "VERDICT: REQUEST_CHANGES":
    print("FAIL: last line of REVIEW.md must be 'VERDICT: REQUEST_CHANGES'", file=sys.stderr)
    sys.exit(1)

DEFECT_LINE = 131
TOLERANCE = 3
LOW_LINE = DEFECT_LINE - TOLERANCE
HIGH_LINE = DEFECT_LINE + TOLERANCE

table_rows = [ln.strip() for ln in lines if ln.strip().startswith("|") and ln.strip().endswith("|")]
if len(table_rows) < 3:
    print("FAIL: REVIEW.md must include a markdown taxonomy table with header, separator, and finding row", file=sys.stderr)
    sys.exit(1)

header_cells = [c.strip().lower() for c in table_rows[0].strip("|").split("|")]
sep_row = table_rows[1]
if not re.match(r"^\|(\s*:?-{3,}:?\s*\|)+$", sep_row):
    print("FAIL: malformed markdown taxonomy table separator", file=sys.stderr)
    sys.exit(1)

required_headers = ("category", "severity")
for req_h in required_headers:
    if not any(req_h in h for h in header_cells):
        print(f"FAIL: taxonomy table missing '{req_h}' column", file=sys.stderr)
        sys.exit(1)
if not any("file" in h or "line" in h for h in header_cells):
    print("FAIL: taxonomy table missing 'file:line' column", file=sys.stderr)
    sys.exit(1)

cat_idx = next(i for i, h in enumerate(header_cells) if "category" in h)
sev_idx = next(i for i, h in enumerate(header_cells) if "severity" in h)
loc_idx = next(i for i, h in enumerate(header_cells) if ("file" in h or "line" in h))
prob_idx = next((i for i, h in enumerate(header_cells) if "problem" in h), None)

VALID_FUNC_CATEGORIES = {"COR", "REQ"}
VALID_BLOCKING_SEVERITIES = {"blocker", "major"}
LOC_RE = re.compile(r"src/link_state_machine\.cpp:(\d+)(?:-(\d+))?")
MAX_LINE_SPAN = 15

matched_row_idx = None
blocker_row_indices = []
for row_idx, row in enumerate(table_rows[2:]):
    cells = [c.strip() for c in row.strip("|").split("|")]
    if len(cells) < len(header_cells):
        continue
    category = cells[cat_idx].strip("` ").upper()
    severity = cells[sev_idx].strip("` ").lower()
    if severity == "blocker":
        blocker_row_indices.append(row_idx)
    if matched_row_idx is not None:
        continue
    loc = cells[loc_idx]
    prob_text = cells[prob_idx] if prob_idx is not None else " ".join(cells)
    row_text = " ".join(cells)

    if category not in VALID_FUNC_CATEGORIES:
        continue
    if severity not in VALID_BLOCKING_SEVERITIES:
        continue
    if "recoverFromFault" not in row_text:
        continue
    if "m_faultCount" not in prob_text and "REQ-LINK-006" not in prob_text:
        continue

    m = LOC_RE.search(loc) or LOC_RE.search(row_text)
    if not m:
        continue
    start_ln = int(m.group(1))
    end_ln = int(m.group(2)) if m.group(2) else start_ln
    if end_ln < start_ln or (end_ln - start_ln + 1) > MAX_LINE_SPAN:
        continue
    if start_ln <= HIGH_LINE and end_ln >= LOW_LINE:
        matched_row_idx = row_idx

if matched_row_idx is None:
    print(
        "FAIL: REVIEW.md must contain a blocker/major COR or REQ finding in the taxonomy table "
        "naming recoverFromFault, m_faultCount or REQ-LINK-006, and a <=15-line range overlapping "
        "src/link_state_machine.cpp:128-134",
        file=sys.stderr,
    )
    sys.exit(1)

extra_blockers = sum(1 for idx in blocker_row_indices if idx != matched_row_idx)
if extra_blockers > 2:
    print(
        f"FAIL: REVIEW.md contains {extra_blockers} extra blocker rows (max 2 allowed)",
        file=sys.stderr,
    )
    sys.exit(1)
PY

echo "PASS: 18-review-fault-recovery"
exit 0
