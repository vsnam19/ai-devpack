#!/usr/bin/env bash
# check.sh - Deterministic grader for eval task 09-doxygen-naming-link (category: doxygen/naming).
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

BASELINE_HASHES=(
    "CMakeLists.txt:02541821df4fc1ed52507179e4c465fb0d167791ba25c9fdb1dc53e6319eda6c"
    "build_and_test.sh:e56e09a56ba4090655bbfe63ff47b361d2920da69b42a76989999f438df5c4da"
    "include/can_codec.hpp:153417bd1a1c0526f9b4542bf7e19653683e3d408ad9503642d14ec8a6773511"
    "include/link_state_machine.hpp:a5f4bd9bbbbd8dc61f142a1ce07985b82faa075d278c54a93ae1645625373dd8"
    "include/ring_buffer.hpp:14512341e1c335f111ef9c7a59443d145efe8206aebe9e27ef50c7b58a416a96"
    "requirements.txt:c26e83b5e63bcaa201021b139166db01946552d6b20f5ceeafc1ddead779305f"
    "src/can_codec.cpp:50e981492e7a66df3acbf7879ba139b36b8700503a2843da0afcdf0380b0198e"
    "src/link_state_machine.cpp:5d527306c3635e9d7050b0c0e32ea99a3d405569839cae45453dbd72529aabe9"
    "src/ring_buffer.cpp:0730cb711223aff7f066aefe601064f2a21949bf8da7dc47d87f2f99b73dfccd"
    "tests/test_can_codec.cpp:a5d01a92642a5068c00e2688127a712b1f70b8d7096dc67329aa53384124a710"
    "tests/test_framework.hpp:5df06254c718fb793cd1d08a4fd173b124f9b706340435acafe7846db3a55063"
    "tests/test_link_state_machine.cpp:6ee4c883cd46d9a1cc414e807a840b3c269d6ff3df23e92b02eb77601ec094a9"
    "tests/test_main.cpp:4c4c3ced27510145251f52cb5a3826c165390c5d9e8bfacdebeed874eda50ff2"
    "tests/test_ring_buffer.cpp:8e4451cb6089f4f63007ae42e649d01490586a04d66048bba7e19c9b802d1df3"
)

ALLOWED_FILES=(
    "include/link_state_machine.hpp"
    "src/link_state_machine.cpp"
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
    fi
done < <(find "$WORKDIR/include" "$WORKDIR/src" "$WORKDIR/tests" -type f | sort)

if [[ "$CHANGED_COUNT" -eq 0 ]]; then
    echo "FAIL: no allowed files were modified or produced" >&2
    exit 1
fi

# 2. Build and run fixture unit tests
bash "$WORKDIR/build_and_test.sh" --no-cmake >/dev/null

# 3. Compile and run hidden test suite
TMP_GRADE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/devpack-grade-09-doxygen-naming-link.XXXXXX")"
# shellcheck disable=SC2317,SC2329
cleanup_grade() {
    rm -rf "$TMP_GRADE_DIR"
}
trap cleanup_grade EXIT

CXX_BIN="${CXX:-g++}"
CXXFLAGS=(-std=c++17 -Wall -Wextra -Werror -Wpedantic -Wswitch-enum -Wunreachable-code -fno-exceptions -fno-rtti)

"$CXX_BIN" "${CXXFLAGS[@]}" \
    -I"$WORKDIR/include" \
    -I"$WORKDIR/tests" \
    "$WORKDIR/src/can_codec.cpp" \
    "$WORKDIR/src/ring_buffer.cpp" \
    "$WORKDIR/src/link_state_machine.cpp" \
    "$HIDDEN_DIR/hidden_test.cpp" \
    -o "$TMP_GRADE_DIR/hidden_test"
"$TMP_GRADE_DIR/hidden_test" >/dev/null

# 4. Task-specific checks
python3 - "$WORKDIR" <<'PY'
import pathlib
import re
import sys

workdir = pathlib.Path(sys.argv[1])
hdr = (workdir / "include/link_state_machine.hpp").read_text(encoding="utf-8")
src = (workdir / "src/link_state_machine.cpp").read_text(encoding="utf-8")

for name, content in [("include/link_state_machine.hpp", hdr), ("src/link_state_machine.cpp", src)]:
    if "@file" not in content or "@brief" not in content:
        print(f"FAIL: missing @file or @brief in {name}", file=sys.stderr)
        sys.exit(1)
    if re.search(r"@param\b(?!\s*\[(?:in|out|in,out)\])", content):
        print(f"FAIL: bare @param without [in]/[out]/[in,out] in {name}", file=sys.stderr)
        sys.exit(1)
    stripped = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/", "", content)
    for bad in ("tmp", "temp", "val", "var", "foo", "bar", "baz", "obj", "dat", "buf", "ptr", "cfg_", "st_", "ts_", "retries_"):
        if re.search(rf"\b{bad}\b", stripped):
            print(f"FAIL: non-compliant identifier '{bad}' found in {name}", file=sys.stderr)
            sys.exit(1)

for member in ("m_config", "m_state", "m_lastTimestampMs", "m_retryCount"):
    if not re.search(rf"\b{member}\b", hdr):
        print(f"FAIL: expected private member '{member}' in include/link_state_machine.hpp", file=sys.stderr)
        sys.exit(1)

for fn_name in ("startConnect", "onHandshakeAck", "onHeartbeat", "onTick"):
    pattern = rf"/\*\*([\s\S]*?)\*/\s*(?:bool|LinkState)\s+{fn_name}\s*\("
    m = re.search(pattern, hdr)
    if not m:
        print(f"FAIL: missing Doxygen block before {fn_name} in include/link_state_machine.hpp", file=sys.stderr)
        sys.exit(1)
    doc_block = m.group(1)
    if "@brief" not in doc_block or "@return" not in doc_block or "@param[in]" not in doc_block:
        print(f"FAIL: incomplete Doxygen (@brief/@param[in]/@return) on {fn_name}", file=sys.stderr)
        sys.exit(1)
PY


echo "PASS: 09-doxygen-naming-link"
exit 0
