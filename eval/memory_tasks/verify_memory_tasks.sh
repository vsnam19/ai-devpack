#!/usr/bin/env bash
# verify_memory_tasks.sh - Offline deterministic verifier for eval/memory_tasks sequences.
#
# Usage: bash eval/memory_tasks/verify_memory_tasks.sh [--help] [--task SEQ_ID]
#
# For every sequence in eval/memory_tasks/<seq-id>/:
#   1. Copies eval/fixture/ to an isolated temporary directory and initializes git.
#   2. Verifies check.sh FAILS on the unmodified baseline.
#   3. Exercises deterministic memory retrieval / proposal acceptance against
#      sequence.toml (expected_ids, forbidden_ids, token budget, scope isolation).
#   4. Verifies wrong_memory.patch or no_memory.patch (when memory is disabled)
#      FAILS check.sh.
#   5. Verifies solution.patch with reference memory PASSES check.sh.
#
# Exit codes:
#   0 - All memory sequences verified successfully
#   1 - One or more sequences failed verification
#   2 - Invalid command-line arguments
set -euo pipefail
# Isolate from the caller's shell: CI and DEVPACK_* settings (base ref, component selection,
# test XML, deviations, baseline SHA, grader nonces, credentials) change results between runs.
unset CI DEVPACK_BASE DEVPACK_COMPONENTS DEVPACK_TEST_XML DEVPACK_DEVIATIONS DEVPACK_GCOV_OBJ_DIR \
  DEVPACK_BASELINE_SHA DEVPACK_AGENT_BIN DEVPACK_EVAL_AGENT_BIN DEVPACK_BROKER \
  DEVPACK_CLAUDE_CREDENTIALS DEVPACK_CPPCHECK_BIN DEVPACK_DIR DEVPACK_FRAMEWORK_GUARD \
  DEVPACK_HARNESS_MARKER_DIR DEVPACK_HARNESS_MARKER_NONCE DEVPACK_HARNESS_NONCE \
  DEVPACK_HARNESS_NONCE_FILE DEVPACK_LOCAL_AUTH_TOKEN DEVPACK_LOCAL_BASE_URL \
  DEVPACK_OPERATOR_CONFIG DEVPACK_SECRET_PATTERNS DEVPACK_TEST_PROOF_DIR DEVPACK_TESTS_COMPLETED \
  DEVPACK_VENDOR_DIR
unset DEVPACK_PROJECT_CONFIG

usage() {
    sed -n '2,/^set -euo pipefail/{ /^set -euo pipefail/d; p; }' "$0" | sed 's/^# \{0,1\}//'
}

FILTER_SEQ=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --task)
            if [[ $# -lt 2 ]]; then
                echo "error: --task requires a sequence ID" >&2
                exit 2
            fi
            FILTER_SEQ="$2"
            shift 2
            ;;
        *)
            echo "error: unknown argument '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done

MEM_TASKS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_ROOT="$(cd "$MEM_TASKS_ROOT/../.." && pwd)"
FIXTURE_ROOT="$(cd "$MEM_TASKS_ROOT/../fixture" && pwd)"

if [[ ! -d "$FIXTURE_ROOT" ]]; then
    echo "error: fixture directory not found at $FIXTURE_ROOT" >&2
    exit 1
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/devpack-verify-memory.XXXXXX")"
# shellcheck disable=SC2317,SC2329
cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

printf "%-28s %-16s %-14s %-14s %-14s %-8s\n" "SEQUENCE" "CATEGORY" "RETRIEVAL" "WRONG_OR_NOMEM" "AFTER_SOLVE" "RESULT"
printf "%-28s %-16s %-14s %-14s %-14s %-8s\n" "----------------------------" "----------------" "--------------" "--------------" "--------------" "--------"

TOTAL_SEQS=0
FAILED_SEQS=0

for seq_dir in "$MEM_TASKS_ROOT"/*/; do
    [[ -d "$seq_dir" ]] || continue
    seq_id="$(basename "$seq_dir")"
    if [[ -n "$FILTER_SEQ" && "$seq_id" != "$FILTER_SEQ" ]]; then
        continue
    fi
    [[ -f "$seq_dir/sequence.toml" ]] || continue

    TOTAL_SEQS=$((TOTAL_SEQS + 1))
    category="$(awk -F'"' '/^category =/ {print $2; exit}' "$seq_dir/sequence.toml")"
    work_dir="$TMP_ROOT/$seq_id"
    mkdir -p "$work_dir"
    cp -r "$FIXTURE_ROOT/." "$work_dir/"

    (
        cd "$work_dir"
        git init -q
        git config user.email "verify@example.invalid"
        git config user.name "Memory Verifier"
        git add -A
        git commit -q -m "chore: base fixture"
    )
    base_sha="$(git -C "$work_dir" rev-parse HEAD)"

    # 1. Baseline must fail check.sh
    baseline_ok=1
    if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" DEVPACK_BASELINE_SHA="$base_sha" bash "$seq_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
        baseline_ok=0
    fi

    # 2. Seed memory or process reference Step 1 proposals
    mem_dir="$work_dir/.devpack/memory"
    # The verifier owns the seed memory, which is outside the worker's file scope.
    printf '\n.devpack/\n' >> "$work_dir/.git/info/exclude"
    mkdir -p "$mem_dir"
    if [[ -d "$seq_dir/seed_memory" ]]; then
        cp -r "$seq_dir/seed_memory/." "$mem_dir/"
    fi
    if [[ -f "$seq_dir/step1_proposals.md" ]]; then
        python3 - "$PACK_ROOT" "$mem_dir" "$seq_dir/step1_proposals.md" "$seq_id" <<'PY'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from runner.memory import process_memory_proposals
prop_text = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8")
res = process_memory_proposals(sys.argv[2], prop_text, source="run:verify-step1", task_id=sys.argv[4])
assert res.accepted, "expected auto-accepted handoff proposal"
PY
    fi

    # 3. Deterministic retrieval verification for every [step_*] in sequence.toml
    retrieval_status="FAIL(BAD)"
    if python3 - "$PACK_ROOT" "$seq_dir" "$mem_dir" <<'PY'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from runner.agent_exec import _minimal_toml_loads
from runner.memory import retrieve_memory

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

seq_dir = pathlib.Path(sys.argv[2])
mem_dir = pathlib.Path(sys.argv[3])
raw_toml = (seq_dir / "sequence.toml").read_text(encoding="utf-8")
data = tomllib.loads(raw_toml) if tomllib else _minimal_toml_loads(raw_toml)

step_keys = sorted(
    (k for k in data if k.startswith("step_")),
    key=lambda s: int(s.split("_", 1)[1]),
)
for skey in step_keys:
    step = data[skey]
    prompt_text = (seq_dir / step["prompt_file"]).read_text(encoding="utf-8")
    role = step.get("role", "implementer")
    engine = step.get("engine", "haiku")
    expected = list(step.get("expected_ids", []))
    if step.get("expect_prior_proposals"):
        expected.append("mem-dtc-freeze-sentinel")
    forbidden = set(step.get("forbidden_ids", []))

    res = retrieve_memory(mem_dir, prompt_text, role=role, engine=engine, task_id=seq_dir.name)
    got = set(res.ids)
    for eid in expected:
        assert eid in got, f"{seq_dir.name}/{skey}: missing expected_id {eid} (got {res.ids})"
    bad = got & forbidden
    assert not bad, f"{seq_dir.name}/{skey}: injected forbidden_ids {sorted(bad)}"
    assert res.total_tokens <= res.budget_tokens, f"{seq_dir.name}/{skey}: exceeded token budget"

    # Recall sequence must have 0 injected entries when memory is disabled
    if data.get("category") == "recall" and skey == "step_2":
        res_disabled = retrieve_memory(mem_dir, prompt_text, role=role, engine=engine, top_k=0)
        assert res_disabled.ids == [], "disabled memory must inject nothing"
PY
    then
        retrieval_status="PASS(OK)"
    fi

    # 4. Negative patch (no_memory.patch or wrong_memory.patch) must FAIL check.sh
    neg_status="MISSING"
    neg_patch=""
    if [[ -f "$seq_dir/no_memory.patch" ]]; then
        neg_patch="$seq_dir/no_memory.patch"
    elif [[ -f "$seq_dir/wrong_memory.patch" ]]; then
        neg_patch="$seq_dir/wrong_memory.patch"
    fi
    if [[ -n "$neg_patch" ]]; then
        git -C "$work_dir" apply "$neg_patch"
        if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" DEVPACK_BASELINE_SHA="$base_sha" bash "$seq_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
            neg_status="PASS(BAD)"
        else
            neg_status="FAIL(OK)"
        fi
        git -C "$work_dir" reset -q --hard HEAD
        git -C "$work_dir" clean -q -fd
    fi

    # 5. Reference solution.patch must PASS check.sh
    solve_status="FAIL(BAD)"
    git -C "$work_dir" apply "$seq_dir/solution.patch"
    if (cd "$work_dir" && DEVPACK_ROOT="$PACK_ROOT" DEVPACK_BASELINE_SHA="$base_sha" bash "$seq_dir/check.sh" --workdir "$work_dir" >/dev/null 2>&1); then
        solve_status="PASS(OK)"
    fi

    if [[ "$baseline_ok" -eq 1 && "$retrieval_status" == "PASS(OK)" && "$neg_status" == "FAIL(OK)" && "$solve_status" == "PASS(OK)" ]]; then
        res_label="OK"
    else
        res_label="FAIL"
        FAILED_SEQS=$((FAILED_SEQS + 1))
    fi

    printf "%-28s %-16s %-14s %-14s %-14s %-8s\n" "$seq_id" "$category" "$retrieval_status" "$neg_status" "$solve_status" "$res_label"
done

echo ""
echo "Verified $TOTAL_SEQS memory sequence(s), $FAILED_SEQS failure(s)."

if [[ "$TOTAL_SEQS" -lt 5 || "$FAILED_SEQS" -gt 0 ]]; then
    exit 1
fi
exit 0
