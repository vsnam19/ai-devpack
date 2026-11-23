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

SAFE_GIT_SH="${DEVPACK_ROOT:+$DEVPACK_ROOT/eval/lib/safe_git.sh}"
if [[ -z "$SAFE_GIT_SH" || ! -f "$SAFE_GIT_SH" ]]; then
    SAFE_GIT_SH="$SCRIPT_DIR/../../lib/safe_git.sh"
fi
# shellcheck source=/dev/null
source "$SAFE_GIT_SH"

BASELINE_HASHES=(
    "CMakeLists.txt:22783b0ea0f6c9bc4e338c480c42effbaedf61b75340eff2a088174c143ab4b6"
    "include/dtc_debouncer.hpp:90c8aee02c289431b8992b2f8cb76f1481d56413222fb34d20ca93ffff68dc80"
    "build_and_test.sh:1a3b151dea5fba7d979b2ee8c947d9893a2ce231b9016dc48b9d915d76206354"
    "include/can_codec.hpp:153417bd1a1c0526f9b4542bf7e19653683e3d408ad9503642d14ec8a6773511"
    "include/link_state_machine.hpp:a5f4bd9bbbbd8dc61f142a1ce07985b82faa075d278c54a93ae1645625373dd8"
    "include/ring_buffer.hpp:14512341e1c335f111ef9c7a59443d145efe8206aebe9e27ef50c7b58a416a96"
    "requirements.txt:b72c524308ed1186ff63a5691a01abdd1316a12014a0ff30790188f92b030a14"
    "src/can_codec.cpp:50e981492e7a66df3acbf7879ba139b36b8700503a2843da0afcdf0380b0198e"
    "src/link_state_machine.cpp:5d527306c3635e9d7050b0c0e32ea99a3d405569839cae45453dbd72529aabe9"
    "src/ring_buffer.cpp:0730cb711223aff7f066aefe601064f2a21949bf8da7dc47d87f2f99b73dfccd"
    "tests/test_can_codec.cpp:a5d01a92642a5068c00e2688127a712b1f70b8d7096dc67329aa53384124a710"
    "tests/test_framework.hpp:58b52cd09a1e9f16339aa79553e78116f5d69acbfd08be4764733df5eef0fdda"
    "tests/test_link_state_machine.cpp:6ee4c883cd46d9a1cc414e807a840b3c269d6ff3df23e92b02eb77601ec094a9"
    "src/dtc_debouncer.cpp:46678c09a3ac7c083d5f12cf8320f08b7340ef72e9ec9899e1e23a4aa117ef45"
    "tests/test_dtc_debouncer.cpp:fb5c745929072ea96d588c7802dda277d02876d3caeb64e1ed215180b744d117"
    "tests/test_main.cpp:c68da9c36c3c6484ff5c52ec6c6b2ead05193da4dbc8d08c2680fb0ed9853723"
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

# Reject .gitignore and run preflight_lint + cppcheck BEFORE running any worker code
run_static_lint_on_files "$WORKDIR" "${ALLOWED_FILES[@]}"

# 2. Build and run fixture unit tests with out-of-process gcov proof
run_gcov_visible_tests "visible build_and_test" "$WORKDIR"

# 3. Compile and run hidden test suite with out-of-process gcov proof
run_gcov_hidden_tests "hidden_test" "$WORKDIR" "$HIDDEN_DIR"

# 4. Task-specific checks
python3 - "$WORKDIR" <<'PY'
import pathlib
import re
import sys

workdir = pathlib.Path(sys.argv[1])
hdr = (workdir / "include/link_state_machine.hpp").read_text(encoding="utf-8")
src = (workdir / "src/link_state_machine.cpp").read_text(encoding="utf-8")

DENYLIST = {"tmp", "temp", "val", "var", "foo", "bar", "baz", "obj", "dat", "buf", "ptr"}
LEGACY_BAD = ("cfg_", "st_", "ts_", "retries_")
CAMEL_CASE_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")
PRIVATE_MEMBER_RE = re.compile(r"^m_[a-z][a-zA-Z0-9]*$")
LOOP_INDICES = {"i", "j", "k"}
CONTROL_PREFIXES = ("if", "else", "while", "switch", "case", "default", "return", "break", "continue", "using", "namespace", "typedef", "static_assert", "#")
VAR_DECL_RE = re.compile(
    r"^(?:(?:const|constexpr|static|mutable)\s+)*"
    r"(?:auto\b|(?:(?:unsigned|signed|long|short)\s+)*(?:std::)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:\s*<[^;{}]+>)?)"
    r"(?:\s*[*&]+|\s+[*&]*)\s*([A-Za-z_]\w*)\s*(?:=|;|\[|\(|\{|$)"
)


def strip_comments_keep_len(text: str) -> str:
    return re.sub(
        r"//[^\n]*|/\*[\s\S]*?\*/",
        lambda m: re.sub(r"[^\n]", " ", m.group(0)),
        text,
    )


def validate_meaningful_name(ident: str, kind: str, filename: str, *, allow_loop: bool = False) -> None:
    if allow_loop and ident in LOOP_INDICES:
        return
    if len(ident) < 3:
        print(f"FAIL: non-meaningful short {kind} '{ident}' (< 3 chars) in {filename}", file=sys.stderr)
        sys.exit(1)
    if ident.lower() in DENYLIST:
        print(f"FAIL: non-meaningful {kind} '{ident}' (denylisted) in {filename}", file=sys.stderr)
        sys.exit(1)
    if not CAMEL_CASE_RE.fullmatch(ident):
        print(f"FAIL: non-camelCase {kind} '{ident}' in {filename}", file=sys.stderr)
        sys.exit(1)


def extract_param_names(param_str: str) -> list[str]:
    stripped = param_str.strip()
    if not stripped or stripped == "void":
        return []
    parts = [p.strip() for p in stripped.split(",")]
    names = []
    for p in parts:
        p_no_default = p.split("=", 1)[0].strip()
        tokens = re.findall(r"[A-Za-z_]\w*", p_no_default)
        if len(tokens) < 2:
            return []
        names.append(tokens[-1])
    return names


for name, content in [("include/link_state_machine.hpp", hdr), ("src/link_state_machine.cpp", src)]:
    if "@file" not in content or "@brief" not in content:
        print(f"FAIL: missing @file or @brief in {name}", file=sys.stderr)
        sys.exit(1)
    if re.search(r"@param\b(?!\s*\[(?:in|out|in,out)\])", content):
        print(f"FAIL: bare @param without [in]/[out]/[in,out] in {name}", file=sys.stderr)
        sys.exit(1)
    stripped = strip_comments_keep_len(content)
    for bad in LEGACY_BAD:
        if re.search(rf"\b{bad}\b", stripped):
            print(f"FAIL: non-compliant identifier '{bad}' found in {name}", file=sys.stderr)
            sys.exit(1)

for member in ("m_config", "m_state", "m_lastTimestampMs", "m_retryCount"):
    if not re.search(rf"\b{member}\b", hdr):
        print(f"FAIL: expected private member '{member}' in include/link_state_machine.hpp", file=sys.stderr)
        sys.exit(1)

hdr_code = strip_comments_keep_len(hdr)
priv_match = re.search(r"\bprivate\s*:([\s\S]*?)\};", hdr_code)
if priv_match:
    for stmt in priv_match.group(1).split(";"):
        s = " ".join(stmt.split())
        if not s:
            continue
        m_field = VAR_DECL_RE.match(s)
        if m_field:
            fname = m_field.group(1)
            if not PRIVATE_MEMBER_RE.fullmatch(fname):
                print(f"FAIL: private member '{fname}' must match m_<camelCase> in include/link_state_machine.hpp", file=sys.stderr)
                sys.exit(1)
            validate_meaningful_name(fname[2:], "private member", "include/link_state_machine.hpp")

src_code = strip_comments_keep_len(src)
for fn_m in re.finditer(
    r"(?:^|\n)[ \t]*(?:[\w:<>*&\s]+?\s+)?([A-Za-z_]\w*)\s*\(([^()]*)\)\s*(?:const\s*)?(?:noexcept\s*)?(?::[^{]*)?\{",
    src_code,
):
    if fn_m.group(1) in ("if", "for", "while", "switch", "catch"):
        continue
    for pname in extract_param_names(fn_m.group(2)):
        validate_meaningful_name(pname, "parameter", "src/link_state_machine.cpp")

    idx = fn_m.end()
    depth = 1
    body_start = idx
    while idx < len(src_code) and depth > 0:
        if src_code[idx] == "{":
            depth += 1
        elif src_code[idx] == "}":
            depth -= 1
        idx += 1
    body = src_code[body_start : idx - 1]
    for stmt in re.split(r"[;{}]", body):
        s = " ".join(stmt.split())
        if not s:
            continue
        if s.startswith("for ") or s.startswith("for("):
            inner = s[s.find("(") + 1 :] if "(" in s else ""
            m_var = VAR_DECL_RE.match(inner.strip())
            if m_var:
                validate_meaningful_name(m_var.group(1), "loop variable", "src/link_state_machine.cpp", allow_loop=True)
            continue
        if any(s == kw or s.startswith(kw + " ") or s.startswith(kw + "(") for kw in CONTROL_PREFIXES):
            continue
        m_var = VAR_DECL_RE.match(s)
        if m_var:
            vname = m_var.group(1)
            if vname not in ("true", "false", "nullptr", "else"):
                validate_meaningful_name(vname, "variable", "src/link_state_machine.cpp")

doc_blocks = list(re.finditer(r"/\*\*([\s\S]*?)\*/", hdr))

targets = [
    ("LinkStateMachine", True, r"(?:explicit\s+)?LinkStateMachine"),
    ("isConfigValid", False, r"bool\s+isConfigValid"),
    ("startConnect", False, r"bool\s+startConnect"),
    ("onHandshakeAck", False, r"bool\s+onHandshakeAck"),
    ("onHeartbeat", False, r"bool\s+onHeartbeat"),
    ("onTick", False, r"LinkState\s+onTick"),
    ("reset", True, r"void\s+reset"),
    ("state", False, r"LinkState\s+state"),
    ("retryCount", False, r"std::uint8_t\s+retryCount"),
    ("lastTimestampMs", False, r"std::uint64_t\s+lastTimestampMs"),
]

prefix_re = r"(?:(?:\[\[[^\]]*\]\]|inline|constexpr|static)\s+)*"
for fn_name, is_void, sig_prefix in targets:
    decl_re = re.compile(rf"(?:^|\n)[ \t]*{prefix_re}{sig_prefix}\s*\(([\s\S]*?)\)\s*(?:const\s+)?noexcept\s*;")
    dm = decl_re.search(hdr_code)
    if not dm:
        print(f"FAIL: missing declaration for {fn_name} in include/link_state_machine.hpp", file=sys.stderr)
        sys.exit(1)
    decl_start = dm.start()
    if hdr_code[decl_start:decl_start + 1] == "\n":
        decl_start += 1
    preceding = [b for b in doc_blocks if b.end() <= decl_start]
    if not preceding:
        print(f"FAIL: missing Doxygen block before {fn_name} in include/link_state_machine.hpp", file=sys.stderr)
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
    for pname in decl_params:
        validate_meaningful_name(pname, "parameter", "include/link_state_machine.hpp")
    doc_params = re.findall(r"@param\s*\[(?:in|out|in,out)\]\s+([A-Za-z_]\w*)", doc_block)
    if doc_params != decl_params:
        print(f"FAIL: @param names {doc_params} do not match declared parameters {decl_params} on {fn_name}", file=sys.stderr)
        sys.exit(1)
PY


echo "PASS: 09-doxygen-naming-link"
exit 0
