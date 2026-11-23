#!/usr/bin/env bash
# check.sh - Deterministic grader for eval task 08-doxygen-naming-codec (category: doxygen/naming).
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
    "CMakeLists.txt:f4c74df4499acb3025e609acb21428e38c5336ca59b26d3728c73d4d0da33c75"
    "include/dtc_debouncer.hpp:90c8aee02c289431b8992b2f8cb76f1481d56413222fb34d20ca93ffff68dc80"
    "build_and_test.sh:f87d8c42f649a0d0a11f7fd6e48068da4ac661edb5463e424ca259e6551a6af0"
    "include/can_codec.hpp:2504898f4f60f1c8d444be3edaaf3fdc076f7482724c723ab6d7605d8c3cca08"
    "include/link_state_machine.hpp:6a73630a93bbbacbdbd19ba4314e4e63d079e2993b193bf53312d04fd9e8bf30"
    "include/ring_buffer.hpp:14512341e1c335f111ef9c7a59443d145efe8206aebe9e27ef50c7b58a416a96"
    "requirements.txt:b72c524308ed1186ff63a5691a01abdd1316a12014a0ff30790188f92b030a14"
    "src/can_codec.cpp:caedbd6f7adfae7ff24facdc1318d331db801f473ff8c769a0b23d2b605e9c89"
    "src/link_state_machine.cpp:72114142bb41abd99fb4e9bd47594fd529c4cae92981474bc037b4a3b2b2d992"
    "src/ring_buffer.cpp:0730cb711223aff7f066aefe601064f2a21949bf8da7dc47d87f2f99b73dfccd"
    "tests/test_can_codec.cpp:a5d01a92642a5068c00e2688127a712b1f70b8d7096dc67329aa53384124a710"
    "tests/test_framework.hpp:63c415272ae5a24fc8cb35dbdd59f94b94f3aba0c36782194e87f641aa7fac78"
    "tests/test_link_state_machine.cpp:6ee4c883cd46d9a1cc414e807a840b3c269d6ff3df23e92b02eb77601ec094a9"
    "src/dtc_debouncer.cpp:46678c09a3ac7c083d5f12cf8320f08b7340ef72e9ec9899e1e23a4aa117ef45"
    "tests/test_dtc_debouncer.cpp:fb5c745929072ea96d588c7802dda277d02876d3caeb64e1ed215180b744d117"
    "tests/test_main.cpp:01b1c4a1105e7461d07c468e37e5a70fc019197a25c8e9cabb76a1737a9a47e6"
    "tests/test_ring_buffer.cpp:8e4451cb6089f4f63007ae42e649d01490586a04d66048bba7e19c9b802d1df3"
)

ALLOWED_FILES=(
    "include/can_codec.hpp"
    "src/can_codec.cpp"
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
FIXTURE_TESTS_DIR="${DEVPACK_ROOT:+$DEVPACK_ROOT/eval/fixture/tests}"
[[ -n "$FIXTURE_TESTS_DIR" && -d "$FIXTURE_TESTS_DIR" ]] || FIXTURE_TESTS_DIR="$SCRIPT_DIR/../../fixture/tests"
[[ -d "$FIXTURE_TESTS_DIR" ]] || FIXTURE_TESTS_DIR="$WORKDIR/tests"
expected_visible="$(count_expected_checks "$FIXTURE_TESTS_DIR"/*.cpp)"
run_verified_test_binary "visible build_and_test" "$expected_visible" \
    bash "$WORKDIR/build_and_test.sh" --no-cmake

# 3. Compile and run hidden test suite
TMP_GRADE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/devpack-grade-08-doxygen-naming-codec.XXXXXX")"
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
    "$WORKDIR/src/dtc_debouncer.cpp" \
    "$HIDDEN_DIR/hidden_test.cpp" \
    -o "$TMP_GRADE_DIR/hidden_test"
expected_hidden="$(count_expected_checks "$HIDDEN_DIR/hidden_test.cpp")"
run_verified_test_binary "hidden_test" "$expected_hidden" "$TMP_GRADE_DIR/hidden_test"

# 4. Task-specific checks
python3 - "$WORKDIR" <<'PY'
import pathlib
import re
import sys

workdir = pathlib.Path(sys.argv[1])
hdr = (workdir / "include/can_codec.hpp").read_text(encoding="utf-8")
src = (workdir / "src/can_codec.cpp").read_text(encoding="utf-8")

for name, content in [("include/can_codec.hpp", hdr), ("src/can_codec.cpp", src)]:
    if "@file" not in content or "@brief" not in content:
        print(f"FAIL: missing @file or @brief in {name}", file=sys.stderr)
        sys.exit(1)
    if re.search(r"@param\b(?!\s*\[(?:in|out|in,out)\])", content):
        print(f"FAIL: bare @param without [in]/[out]/[in,out] in {name}", file=sys.stderr)
        sys.exit(1)
    stripped = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/", "", content)
    for bad in ("tmp", "temp", "val", "var", "foo", "bar", "baz", "obj", "dat", "buf", "ptr"):
        if re.search(rf"\b{bad}\b", stripped):
            print(f"FAIL: non-meaningful identifier '{bad}' found in {name}", file=sys.stderr)
            sys.exit(1)

def strip_comments_keep_len(text: str) -> str:
    return re.sub(
        r"//[^\n]*|/\*[\s\S]*?\*/",
        lambda m: re.sub(r"[^\n]", " ", m.group(0)),
        text,
    )

def extract_param_names(param_str: str) -> list[str]:
    stripped = param_str.strip()
    if not stripped or stripped == "void":
        return []
    parts = [p.strip() for p in stripped.split(",")]
    names = []
    for p in parts:
        p_no_default = p.split("=", 1)[0].strip()
        tokens = re.findall(r"[A-Za-z_]\w*", p_no_default)
        if not tokens:
            return []
        names.append(tokens[-1])
    return names

hdr_code = strip_comments_keep_len(hdr)
doc_blocks = list(re.finditer(r"/\*\*([\s\S]*?)\*/", hdr))

targets = [
    ("isValidSignal", False),
    ("packRaw", False),
    ("unpackRaw", False),
    ("signExtend", False),
    ("encodePhysical", False),
    ("decodePhysical", False),
]

for fn_name, is_void in targets:
    decl_re = re.compile(rf"(?:^|\n)[ \t]*(?:bool|std::int64_t)\s+{fn_name}\s*\(([\s\S]*?)\)\s*noexcept\s*;")
    dm = decl_re.search(hdr_code)
    if not dm:
        print(f"FAIL: missing declaration for {fn_name} in include/can_codec.hpp", file=sys.stderr)
        sys.exit(1)
    decl_start = dm.start()
    if hdr_code[decl_start:decl_start + 1] == "\n":
        decl_start += 1
    preceding = [b for b in doc_blocks if b.end() <= decl_start]
    if not preceding:
        print(f"FAIL: missing Doxygen comment block before {fn_name} in include/can_codec.hpp", file=sys.stderr)
        sys.exit(1)
    nearest = preceding[-1]
    gap = hdr_code[nearest.end():decl_start]
    if gap.strip():
        print(f"FAIL: non-whitespace code between nearest Doxygen block and {fn_name}", file=sys.stderr)
        sys.exit(1)
    doc_block = nearest.group(1)
    if not re.search(r"@brief\s+\S+", doc_block):
        print(f"FAIL: missing @brief on {fn_name}", file=sys.stderr)
        sys.exit(1)
    if not is_void and not re.search(r"@return\s+\S+", doc_block):
        print(f"FAIL: missing @return on non-void {fn_name}", file=sys.stderr)
        sys.exit(1)
    decl_params = extract_param_names(dm.group(1))
    doc_params = re.findall(r"@param\s*\[(?:in|out|in,out)\]\s+([A-Za-z_]\w*)", doc_block)
    if doc_params != decl_params:
        print(f"FAIL: @param names {doc_params} do not match declared parameters {decl_params} on {fn_name}", file=sys.stderr)
        sys.exit(1)
PY


echo "PASS: 08-doxygen-naming-codec"
exit 0
