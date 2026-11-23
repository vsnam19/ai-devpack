#!/usr/bin/env bash
# check.sh - Deterministic grader for eval task 06-small-feature-ring-count-id (category: small-feature).
#
# Usage: bash check.sh [--help] [--workdir DIR]
#
# Verifies file scope against post-setup baseline checksums, compiles and runs
# fixture tests and hidden tests, and executes task-specific grading checks.
#
# Exit codes:
#   0 - All checks passed
#   1 - One or more grading checks failed
#   2 - Invalid command-line arguments
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$WORKDIR" ]]; then
    if [[ -f "$PWD/CMakeLists.txt" && -d "$PWD/src" ]]; then
        WORKDIR="$PWD"
    elif [[ -f "$SCRIPT_DIR/CMakeLists.txt" && -d "$SCRIPT_DIR/src" ]]; then
        WORKDIR="$SCRIPT_DIR"
    else
        echo "FAIL: cannot locate fixture workdir (run from task workdir or pass --workdir DIR)" >&2
        exit 1
    fi
fi
WORKDIR="$(cd "$WORKDIR" && pwd)"

HIDDEN_DIR=""
if [[ -d "$SCRIPT_DIR/hidden" ]]; then
    HIDDEN_DIR="$SCRIPT_DIR/hidden"
elif [[ -d "$WORKDIR/hidden" ]]; then
    HIDDEN_DIR="$WORKDIR/hidden"
else
    echo "FAIL: cannot locate hidden test directory" >&2
    exit 1
fi

SAFE_GIT_SH="${DEVPACK_ROOT:+$DEVPACK_ROOT/eval/lib/safe_git.sh}"
if [[ -z "$SAFE_GIT_SH" || ! -f "$SAFE_GIT_SH" ]]; then
    SAFE_GIT_SH="$SCRIPT_DIR/../../lib/safe_git.sh"
fi
# shellcheck source=/dev/null
source "$SAFE_GIT_SH"

BASELINE_HASHES=(
    "CMakeLists.txt:22783b0ea0f6c9bc4e338c480c42effbaedf61b75340eff2a088174c143ab4b6"
    "include/dtc_debouncer.hpp:90c8aee02c289431b8992b2f8cb76f1481d56413222fb34d20ca93ffff68dc80"
    "build_and_test.sh:5838f5702218075be8cb64f690528ad16686fa750422a9de566ef14e62fc30c8"
    "include/can_codec.hpp:153417bd1a1c0526f9b4542bf7e19653683e3d408ad9503642d14ec8a6773511"
    "include/link_state_machine.hpp:6a73630a93bbbacbdbd19ba4314e4e63d079e2993b193bf53312d04fd9e8bf30"
    "include/ring_buffer.hpp:14512341e1c335f111ef9c7a59443d145efe8206aebe9e27ef50c7b58a416a96"
    "requirements.txt:05a20078deb422c01491ecd7ddf5db532d895de11704ae8552d79b29444b1dfd"
    "src/can_codec.cpp:50e981492e7a66df3acbf7879ba139b36b8700503a2843da0afcdf0380b0198e"
    "src/link_state_machine.cpp:72114142bb41abd99fb4e9bd47594fd529c4cae92981474bc037b4a3b2b2d992"
    "src/ring_buffer.cpp:0730cb711223aff7f066aefe601064f2a21949bf8da7dc47d87f2f99b73dfccd"
    "tests/test_can_codec.cpp:a5d01a92642a5068c00e2688127a712b1f70b8d7096dc67329aa53384124a710"
    "tests/test_framework.hpp:d4ac449c6f651f5ea22616cf4564eb63e1130a8d3fafbfa8af5e0f44a2862af2"
    "tests/test_link_state_machine.cpp:6ee4c883cd46d9a1cc414e807a840b3c269d6ff3df23e92b02eb77601ec094a9"
    "src/dtc_debouncer.cpp:46678c09a3ac7c083d5f12cf8320f08b7340ef72e9ec9899e1e23a4aa117ef45"
    "tests/test_dtc_debouncer.cpp:fb5c745929072ea96d588c7802dda277d02876d3caeb64e1ed215180b744d117"
    "tests/test_main.cpp:c68da9c36c3c6484ff5c52ec6c6b2ead05193da4dbc8d08c2680fb0ed9853723"
    "tests/test_ring_buffer.cpp:8e4451cb6089f4f63007ae42e649d01490586a04d66048bba7e19c9b802d1df3"
)

ALLOWED_FILES=(
    "include/ring_buffer.hpp"
    "src/ring_buffer.cpp"
    "tests/test_ring_count_id.cpp"
)

is_allowed_file() {
    local candidate="$1"
    local item
    for item in "${ALLOWED_FILES[@]}"; do
        if [[ "$item" == "$candidate" ]]; then
            return 0
        fi
    done
    return 1
}

# 1. Verify scope and detect modified files relative to post-setup state
CHANGED_COUNT=0
for entry in "${BASELINE_HASHES[@]}"; do
    rel_path="${entry%%:*}"
    expected_sha="${entry##*:}"
    full_path="$WORKDIR/$rel_path"
    if [[ ! -f "$full_path" ]]; then
        echo "FAIL: tracked file '$rel_path' was deleted" >&2
        exit 1
    fi
    actual_sha="$(sha256sum "$full_path" | awk '{print $1}')"
    if [[ "$actual_sha" != "$expected_sha" ]]; then
        if ! is_allowed_file "$rel_path"; then
            echo "FAIL: scope violation - modified disallowed file '$rel_path'" >&2
            exit 1
        fi
        CHANGED_COUNT=$((CHANGED_COUNT + 1))
    fi
done

if is_allowed_file "STOP_REPORT.md" && [[ -f "$WORKDIR/STOP_REPORT.md" ]]; then
    CHANGED_COUNT=$((CHANGED_COUNT + 1))
fi

# Reject unexpected new files inside include/, src/, or tests/
while IFS= read -r existing_file; do
    rel_existing="${existing_file#"$WORKDIR/"}"
    found_in_baseline=0
    for entry in "${BASELINE_HASHES[@]}"; do
        if [[ "${entry%%:*}" == "$rel_existing" ]]; then
            found_in_baseline=1
            break
        fi
    done
    if [[ "$found_in_baseline" -eq 0 ]]; then
        if ! is_allowed_file "$rel_existing"; then
            echo "FAIL: scope violation - unexpected new file '$rel_existing'" >&2
            exit 1
        fi
        CHANGED_COUNT=$((CHANGED_COUNT + 1))
    fi
done < <(find "$WORKDIR/include" "$WORKDIR/src" "$WORKDIR/tests" -type f | sort)

if [[ "$CHANGED_COUNT" -eq 0 ]]; then
    echo "FAIL: no allowed files were modified or produced" >&2
    exit 1
fi

# Reject .gitignore and run preflight_lint + cppcheck BEFORE running any worker code
run_static_lint_on_files "$WORKDIR" "${ALLOWED_FILES[@]}"

# 2. Build and run fixture unit tests with out-of-process gcov proof
run_gcov_visible_tests "visible build_and_test" "$WORKDIR" "$WORKDIR/tests/test_ring_count_id.cpp"

# 3. Compile and run hidden test suite with out-of-process gcov proof
run_gcov_hidden_tests "hidden_test" "$WORKDIR" "$HIDDEN_DIR"

# 4. Task-specific checks
python3 - "$WORKDIR" <<'PY'
import pathlib
import re
import sys

workdir = pathlib.Path(sys.argv[1])
hdr = (workdir / "include/ring_buffer.hpp").read_text(encoding="utf-8")
test_path = workdir / "tests/test_ring_count_id.cpp"
if not test_path.is_file():
    print("FAIL: missing new test file tests/test_ring_count_id.cpp", file=sys.stderr)
    sys.exit(1)
test_text = test_path.read_text(encoding="utf-8")

if not re.search(r"@req\s+REQ-RING-004\b", test_text):
    print("FAIL: missing @req REQ-RING-004 tag in tests/test_ring_count_id.cpp", file=sys.stderr)
    sys.exit(1)
if "REGISTER_TEST_SUITE" not in test_text:
    print("FAIL: tests/test_ring_count_id.cpp must register its suite via REGISTER_TEST_SUITE", file=sys.stderr)
    sys.exit(1)
if re.search(r"@param\b(?!\s*\[(?:in|out|in,out)\])", hdr):
    print("FAIL: @param missing [in]/[out] direction in include/ring_buffer.hpp", file=sys.stderr)
    sys.exit(1)
if not re.search(r"@param\[in\]\s+canId\b", hdr):
    print("FAIL: missing @param[in] canId Doxygen tag in include/ring_buffer.hpp", file=sys.stderr)
    sys.exit(1)
PY

MUTATE_PY="${DEVPACK_ROOT:+$DEVPACK_ROOT/eval/lib/mutate_cpp.py}"
if [[ -z "$MUTATE_PY" || ! -f "$MUTATE_PY" ]]; then
    MUTATE_PY="$SCRIPT_DIR/../../lib/mutate_cpp.py"
fi

# Require the worker's NEW test file (tests/test_ring_count_id.cpp) alone to kill
# mutants covering count matching, CAN ID filtering, wrap-around, and non-mutation.
python3 "$MUTATE_PY" --workdir "$WORKDIR" --test-file "tests/test_ring_count_id.cpp" --spec-stdin <<'JSON'
[
  {
    "name": "countByCanId always returns 0",
    "file": "src/ring_buffer.cpp",
    "mode": "replace_body",
    "func_regex": "\\bFrameRingBuffer\\s*::\\s*countByCanId\\s*\\([^)]*\\)",
    "body": "{ (void)canId; return 0U; }"
  },
  {
    "name": "countByCanId ignores canId filter and returns m_count",
    "file": "src/ring_buffer.cpp",
    "mode": "replace_body",
    "func_regex": "\\bFrameRingBuffer\\s*::\\s*countByCanId\\s*\\([^)]*\\)",
    "body": "{ (void)canId; return m_count; }"
  },
  {
    "name": "countByCanId fails on wrap-around modulo m_capacity",
    "file": "src/ring_buffer.cpp",
    "mode": "replace_body",
    "func_regex": "\\bFrameRingBuffer\\s*::\\s*countByCanId\\s*\\([^)]*\\)",
    "body": "{\n    std::size_t matches = 0U;\n    for (std::size_t i = 0U; i < m_count; ++i) {\n        const std::size_t idx = m_tail + i;\n        if (idx < m_capacity && m_storage[idx].canId == canId) {\n            ++matches;\n        }\n    }\n    return matches;\n}"
  },
  {
    "name": "countByCanId mutates buffer state by clearing m_count",
    "file": "src/ring_buffer.cpp",
    "mode": "replace_body",
    "func_regex": "\\bFrameRingBuffer\\s*::\\s*countByCanId\\s*\\([^)]*\\)",
    "body": "{\n    std::size_t matches = 0U;\n    for (std::size_t i = 0U; i < m_count; ++i) {\n        const std::size_t idx = (m_tail + i) % m_capacity;\n        if (m_storage[idx].canId == canId) {\n            ++matches;\n        }\n    }\n    const_cast<FrameRingBuffer*>(this)->m_count = 0U;\n    return matches;\n}"
  }
]
JSON


echo "PASS: 06-small-feature-ring-count-id"
exit 0
