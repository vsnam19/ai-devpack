#!/usr/bin/env bash
# safe_git.sh - Hardened git wrapper and environment exports for shell graders.
#
# Usage:
#   source "<DEVPACK_ROOT>/eval/lib/safe_git.sh"
#   safe_git -C "$WORKDIR" diff --name-only "$BASE_REF"
#
# Exports GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null, and
# GIT_TERMINAL_PROMPT=0 on source, verifies repository .git/config via
# runner/safe_git.py when available, and invokes git with hardened -c
# overrides (and --no-ext-diff --no-textconv for diff/show/log).
#
# Exit codes:
#   0 - Helper sourced successfully (or --help printed when executed directly)
#   1 - Unsafe git config detected or git command failed
#   2 - Invalid direct invocation

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -euo pipefail
    if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
        sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
    fi
    echo "error: safe_git.sh is meant to be sourced (try --help)" >&2
    exit 2
fi

export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0
export GIT_NO_REPLACE_OBJECTS=1

_SAFE_GIT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_SAFE_GIT_DEFAULT_ROOT="$(cd "$_SAFE_GIT_LIB_DIR/../.." && pwd)"

safe_git() {
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local safe_git_py="$pack_root/runner/safe_git.py"
    local target_dir="$PWD"
    local subcmd=""
    local subcmd_idx=-1
    local args=("$@")
    local i=0
    local n=${#args[@]}

    while [[ $i -lt $n ]]; do
        local tok="${args[$i]}"
        case "$tok" in
            --)
                break
                ;;
            -C)
                if [[ $((i + 1)) -lt $n ]]; then
                    target_dir="${args[$((i + 1))]}"
                fi
                i=$((i + 2))
                ;;
            -c|--git-dir|--work-tree|--namespace|--super-prefix|--exec-path|--config-env)
                i=$((i + 2))
                ;;
            -C*)
                target_dir="${tok#-C}"
                i=$((i + 1))
                ;;
            -*)
                i=$((i + 1))
                ;;
            *)
                subcmd="$tok"
                subcmd_idx=$i
                break
                ;;
        esac
    done

    if [[ -f "$safe_git_py" ]] && command -v python3 >/dev/null 2>&1; then
        if ! python3 -B "$safe_git_py" verify "$target_dir" >&2; then
            return 1
        fi
    fi

    local final_args=()
    if [[ "$subcmd_idx" -ge 0 ]] && [[ "$subcmd" == "diff" || "$subcmd" == "show" || "$subcmd" == "log" ]]; then
        local j=0
        while [[ $j -lt $n ]]; do
            final_args+=("${args[$j]}")
            if [[ $j -eq $subcmd_idx ]]; then
                final_args+=("--no-ext-diff" "--no-textconv")
            fi
            j=$((j + 1))
        done
    else
        final_args=("${args[@]}")
    fi

    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    GIT_NO_REPLACE_OBJECTS=1 \
    git \
        --no-replace-objects \
        -c core.fsmonitor= \
        -c core.hooksPath=/dev/null \
        -c diff.external= \
        -c core.sshCommand= \
        -c protocol.allow=never \
        -c core.pager=cat \
        "${final_args[@]}"
}

# Count static CHECK(...) / CHECK_EQ(...) invocations across given C++ test files.
count_expected_checks() {
    local total=0
    local f
    for f in "$@"; do
        if [[ -f "$f" ]]; then
            local c=0
            c="$(grep -hEo '(^|[^A-Za-z0-9_])CHECK(_EQ|_TRUE|_FALSE|_NEAR)?[[:space:]]*\(' "$f" 2>/dev/null | wc -l || true)"
            total=$((total + c))
        fi
    done
    echo "$total"
}

# Verify test-run completion proof line on stdout:
#   DEVPACK_TESTS_COMPLETED checks=<n> failed=0 with n >= expected_min (>= 1).
# Usage: verify_test_completion_output LABEL EXPECTED_MIN OUTPUT_FILE
verify_test_completion_output() {
    local label="$1"
    local expected_min="$2"
    local out_file="$3"
    local last_line=""
    last_line="$(awk 'NF{line=$0} END{print line}' "$out_file" | tr -d '\r')"
    if [[ ! "$last_line" =~ ^DEVPACK_TESTS_COMPLETED\ checks=([0-9]+)\ failed=([0-9]+)$ ]]; then
        echo "FAIL: $label did not emit final DEVPACK_TESTS_COMPLETED line (got: '${last_line:-<empty>}')" >&2
        return 1
    fi
    local checks="${BASH_REMATCH[1]}"
    local failed="${BASH_REMATCH[2]}"
    if [[ "$failed" -ne 0 ]]; then
        echo "FAIL: $label reported failed=$failed" >&2
        return 1
    fi
    if [[ "$expected_min" -lt 1 ]]; then
        expected_min=1
    fi
    if [[ "$checks" -lt "$expected_min" ]]; then
        echo "FAIL: $label executed only $checks check(s); expected at least $expected_min" >&2
        return 1
    fi
    return 0
}

# Write the structured pre-candidate marker the wrapper accepts: harness_error.json in
# DEVPACK_HARNESS_MARKER_DIR carrying DEVPACK_HARNESS_MARKER_NONCE, a stage and a reason.
# A no-op unless the wrapper provided both values. Only call this before candidate code runs.
# Usage: write_harness_marker STAGE REASON
write_harness_marker() {
    local stage="$1"
    local reason="$2"
    local marker_dir="${DEVPACK_HARNESS_MARKER_DIR:-}"
    local nonce="${DEVPACK_HARNESS_MARKER_NONCE:-}"
    if [[ -z "$marker_dir" || -z "$nonce" ]]; then
        return 0
    fi
    python3 -I -c '
import json, os, sys
payload = {"nonce": os.environ["DEVPACK_HARNESS_MARKER_NONCE"], "stage": sys.argv[2], "reason": sys.argv[3]}
with open(os.path.join(sys.argv[1], "harness_error.json"), "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload))
' "$marker_dir" "$stage" "$reason" 2>/dev/null || true
}

# Emit GRADER_MISCONFIGURED and exit 2. Exit 2 alone is a candidate failure in the wrapper;
# only a pre-candidate call to grader_misconfigured_at adds a marker that counts as harness error.
grader_misconfigured() {
    echo "GRADER_MISCONFIGURED: $*" >&2
    exit 2
}

# Pre-candidate failure: write the stage marker, then behave like grader_misconfigured.
# Usage: grader_misconfigured_at STAGE MESSAGE...   (STAGE: pack_file | reference_build)
grader_misconfigured_at() {
    local stage="$1"
    shift
    write_harness_marker "$stage" "$*"
    grader_misconfigured "$*"
}

# Enumerate changed and untracked files using scope_check.py (without --exclude-standard).
# Usage: list_changed_files WORKDIR BASE_REF
list_changed_files() {
    local workdir="$1"
    local base_ref="$2"
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local scope_py="$pack_root/enforcement/checks/scope_check.py"
    if [[ ! -f "$scope_py" ]]; then
        grader_misconfigured_at pack_file "missing $scope_py"
    fi
    python3 "$scope_py" --repo "$workdir" --base "$base_ref" --list-changed
}

# Execute a test command/binary and require exit 0 + valid DEVPACK_TESTS_COMPLETED proof.
# Usage: run_verified_test_binary LABEL EXPECTED_MIN CMD [ARGS...]
run_verified_test_binary() {
    local label="$1"
    local expected_min="$2"
    shift 2
    local tmp_out
    tmp_out="$(mktemp "${TMPDIR:-/tmp}/devpack-test-proof.XXXXXX")"
    local rc=0
    # Candidate code runs without the marker nonce or marker directory (see write_harness_marker).
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE -u DEVPACK_HARNESS_MARKER_NONCE -u DEVPACK_HARNESS_MARKER_DIR "$@" >"$tmp_out" 2>&1 || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        cat "$tmp_out" >&2 || true
        rm -f "$tmp_out"
        echo "FAIL: $label exited with status $rc" >&2
        if [[ "$rc" -eq 2 ]]; then
            return 1
        fi
        return "$rc"
    fi
    if ! verify_test_completion_output "$label" "$expected_min" "$tmp_out"; then
        cat "$tmp_out" >&2 || true
        rm -f "$tmp_out"
        return 1
    fi
    rm -f "$tmp_out"
    return 0
}

# Build and run C++ test sources with --coverage on TEST TUs only and pack test_framework.hpp force-included,
# then verify out-of-process gcov coverage of every CHECK* line in the pristine test sources.
# Usage: run_gcov_hidden_tests LABEL WORKDIR HIDDEN_DIR [ASSERT_REGEX]
run_gcov_hidden_tests() {
    local label="$1"
    local workdir="$2"
    local hidden_dir="$3"
    local assert_regex="${4:-}"
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local proof_py="$pack_root/enforcement/checks/test_proof.py"
    local pack_fw="$pack_root/eval/fixture/tests/test_framework.hpp"

    if [[ ! -f "$proof_py" || ! -f "$pack_fw" ]]; then
        grader_misconfigured_at pack_file "missing test_proof.py or test_framework.hpp under DEVPACK_ROOT=$pack_root"
    fi

    local tmp_grade_dir
    tmp_grade_dir="$(mktemp -d "${TMPDIR:-/tmp}/devpack-gcov-hidden.XXXXXX")"
    local cov_dir="$tmp_grade_dir/cov"
    mkdir -p "$tmp_grade_dir/work" "$tmp_grade_dir/work/hidden_tests" "$cov_dir"
    cp -r "$workdir/." "$tmp_grade_dir/work/"
    cp -r "$hidden_dir/." "$tmp_grade_dir/work/hidden_tests/"

    local cxx_bin="${CXX:-g++}"
    local cxxflags=(-std=c++17 -Wall -Wextra -Werror -Wpedantic -Wswitch-enum -Wunreachable-code -fno-exceptions -fno-rtti)
    local guard_macro
    guard_macro="DEVPACK_TF_GUARD_$(date +%s)_$$"

    local src_objs=()
    local src_f
    while IFS= read -r src_f; do
        local obj_f
        obj_f="$tmp_grade_dir/$(basename "${src_f%.cpp}").o"
        if ! "$cxx_bin" "${cxxflags[@]}" -I"$tmp_grade_dir/work/include" -c "$src_f" -o "$obj_f"; then
            rm -rf "$tmp_grade_dir"
            echo "FAIL: $label failed to compile worker source $(basename "$src_f")" >&2
            return 1
        fi
        src_objs+=("$obj_f")
    done < <(find "$tmp_grade_dir/work/src" -maxdepth 1 -type f -name '*.cpp' | sort)

    local hidden_srcs=()
    local test_objs=()
    local h_f
    while IFS= read -r h_f; do
        hidden_srcs+=("$h_f")
        local obj_f
        obj_f="$cov_dir/$(basename "${h_f%.cpp}").o"
        local tf_flags=("-DDEVPACK_FRAMEWORK_GUARD=$guard_macro" "-include" "$pack_fw")
        if [[ -n "$assert_regex" ]]; then
            tf_flags=()
        fi
        if ! "$cxx_bin" "${cxxflags[@]}" --coverage "${tf_flags[@]}" \
            -I"$tmp_grade_dir/work/hidden_tests" \
            -I"$tmp_grade_dir/work/include" \
            -I"$tmp_grade_dir/work/tests" \
            -c "$h_f" -o "$obj_f"; then
            rm -rf "$tmp_grade_dir"
            echo "FAIL: $label failed to compile hidden test $(basename "$h_f")" >&2
            return 1
        fi
        test_objs+=("$obj_f")
    done < <(find "$tmp_grade_dir/work/hidden_tests" -maxdepth 1 -type f \( -name '*.cpp' -o -name '*.c' -o -name '*.cc' \) | sort)

    if [[ "${#hidden_srcs[@]}" -eq 0 ]]; then
        rm -rf "$tmp_grade_dir"
        echo "FAIL: no hidden test source found in $hidden_dir" >&2
        return 1
    fi

    if ! "$cxx_bin" --coverage "${src_objs[@]}" "${test_objs[@]}" -o "$tmp_grade_dir/hidden_test"; then
        rm -rf "$tmp_grade_dir"
        echo "FAIL: $label failed to link hidden_test" >&2
        return 1
    fi

    local proof_args=(run --obj-dir "$cov_dir" --label "$label")
    if [[ -n "$assert_regex" ]]; then
        proof_args+=(--assert-regex "$assert_regex")
    fi
    for h_f in "${hidden_srcs[@]}"; do
        proof_args+=(--test-src "$h_f")
    done
    proof_args+=(-- "$tmp_grade_dir/hidden_test")

    local rc=0
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE -u DEVPACK_HARNESS_MARKER_NONCE -u DEVPACK_HARNESS_MARKER_DIR python3 "$proof_py" "${proof_args[@]}" || rc=$?
    rm -rf "$tmp_grade_dir"
    if [[ "$rc" -eq 3 ]]; then
        grader_misconfigured "gcov TOOL_MISSING"
    elif [[ "$rc" -ne 0 ]]; then
        return 1
    fi
    return 0
}

# Build and run visible fixture tests with --coverage on TEST TUs only and verify gcov proof.
# Usage: run_gcov_visible_tests LABEL WORKDIR [EXTRA_TEST_SRC...]
run_gcov_visible_tests() {
    local label="$1"
    local workdir="$2"
    shift 2
    local extra_tests=("$@")
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local proof_py="$pack_root/enforcement/checks/test_proof.py"
    local fixture_tests_dir="$pack_root/eval/fixture/tests"

    if [[ ! -f "$proof_py" || ! -d "$fixture_tests_dir" ]]; then
        grader_misconfigured_at pack_file "missing test_proof.py or fixture/tests under DEVPACK_ROOT=$pack_root"
    fi

    local tmp_vis_dir
    tmp_vis_dir="$(mktemp -d "${TMPDIR:-/tmp}/devpack-gcov-visible.XXXXXX")"
    local cov_dir="$tmp_vis_dir/cov"
    mkdir -p "$cov_dir"

    local out_file="$tmp_vis_dir/visible.out"
    local test_rc=0
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE -u DEVPACK_HARNESS_MARKER_NONCE -u DEVPACK_HARNESS_MARKER_DIR \
        DEVPACK_ROOT="$pack_root" DEVPACK_GCOV_OBJ_DIR="$cov_dir" \
        bash "$workdir/build_and_test.sh" --no-cmake >"$out_file" 2>&1 || test_rc=$?

    if [[ "$test_rc" -ne 0 ]]; then
        cat "$out_file" >&2 || true
        rm -rf "$tmp_vis_dir"
        echo "FAIL: $label exited with status $test_rc" >&2
        return 1
    fi

    local proof_args=(verify --obj-dir "$cov_dir" --stdout-file "$out_file" --returncode "$test_rc" --label "$label")
    local f
    for f in "$fixture_tests_dir"/*.cpp; do
        [[ -f "$f" ]] && proof_args+=(--test-src "$f")
    done
    for f in "${extra_tests[@]}"; do
        [[ -n "$f" && -f "$f" ]] && proof_args+=(--test-src "$f")
    done

    local proof_rc=0
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE -u DEVPACK_HARNESS_MARKER_NONCE -u DEVPACK_HARNESS_MARKER_DIR \
        python3 "$proof_py" "${proof_args[@]}" || proof_rc=$?
    if [[ "$proof_rc" -eq 3 ]]; then
        rm -rf "$tmp_vis_dir"
        grader_misconfigured "gcov TOOL_MISSING"
    elif [[ "$proof_rc" -ne 0 ]]; then
        cat "$out_file" >&2 || true
        rm -rf "$tmp_vis_dir"
        return 1
    fi
    rm -rf "$tmp_vis_dir"
    return 0
}

# Resolve external baseline commit SHA recorded by the harness; never search commit messages.
# Usage: require_baseline_sha WORKDIR [OPTIONAL_TRIAL_DIR]
require_baseline_sha() {
    local workdir="$1"
    local trial_dir="${2:-${TRIAL_DIR:-}}"
    local sha="${DEVPACK_BASELINE_SHA:-}"
    if [[ -z "$sha" && -n "$trial_dir" && -f "$trial_dir/baseline.sha" ]]; then
        sha="$(head -n 1 "$trial_dir/baseline.sha" | tr -d '[:space:]')"
    fi
    if [[ -z "$sha" || ! "$sha" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
        write_harness_marker reference_build "missing external DEVPACK_BASELINE_SHA (commit-message lookup is disabled)"
        echo "GRADER_MISCONFIGURED: missing external DEVPACK_BASELINE_SHA (commit-message lookup is disabled)" >&2
        return 2
    fi
    if ! safe_git -C "$workdir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        write_harness_marker reference_build "DEVPACK_BASELINE_SHA '$sha' does not resolve to a commit"
        echo "GRADER_MISCONFIGURED: DEVPACK_BASELINE_SHA '$sha' does not resolve to a commit in $workdir" >&2
        return 2
    fi
    echo "$sha"
}

# Reject hidden .gitignore files and run preflight_lint + cppcheck on C/C++ files BEFORE execution.
# Usage: run_static_lint_on_files WORKDIR [FILE...]
run_static_lint_on_files() {
    local workdir="$1"
    shift
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local preflight_py="$pack_root/enforcement/lint/preflight_lint.py"
    local cppcheck_py="$pack_root/enforcement/static/cppcheck_check.py"
    local preflight_cfg="$pack_root/eval/fixture/preflight.toml"

    if [[ ! -f "$preflight_py" || ! -f "$cppcheck_py" || ! -f "$preflight_cfg" ]]; then
        grader_misconfigured_at pack_file "missing preflight_lint.py, cppcheck_check.py, or preflight.toml under DEVPACK_ROOT=$pack_root"
    fi

    if find "$workdir" -maxdepth 4 -path '*/.git' -prune -o -name '.gitignore' -print -quit | grep -q .; then
        echo "FAIL: scope violation - unexpected .gitignore file found in workdir" >&2
        return 1
    fi

    local lint_targets=()
    local f
    for f in "$@"; do
        [[ -z "$f" ]] && continue
        local full_f="$f"
        if [[ "$full_f" != /* ]]; then
            full_f="$workdir/$f"
        fi
        case "$full_f" in
            *.c|*.cc|*.cpp|*.cxx|*.h|*.hh|*.hpp|*.hxx)
                if [[ -f "$full_f" ]]; then
                    lint_targets+=("$full_f")
                fi
                ;;
        esac
    done

    if [[ "${#lint_targets[@]}" -eq 0 ]]; then
        return 0
    fi

    local base_args=()
    if [[ -n "${DEVPACK_BASELINE_SHA:-}" ]]; then
        base_args=(--base "$DEVPACK_BASELINE_SHA")
    elif safe_git -C "$workdir" rev-parse --verify HEAD >/dev/null 2>&1; then
        base_args=(--base HEAD)
    fi

    if ! python3 "$preflight_py" --no-cache --repo-root "$workdir" --config "$preflight_cfg" "${base_args[@]}" "${lint_targets[@]}" >/dev/null; then
        echo "FAIL: preflight_lint found blocking violations" >&2
        return 1
    fi

    local cppcheck_out=""
    local cppcheck_rc=0
    cppcheck_out="$(python3 "$cppcheck_py" --repo-root "$workdir" --config "$preflight_cfg" "${lint_targets[@]}" 2>&1)" || cppcheck_rc=$?
    if [[ "$cppcheck_rc" -eq 3 ]]; then
        grader_misconfigured "cppcheck TOOL_MISSING: $cppcheck_out"
    elif [[ "$cppcheck_rc" -eq 2 ]]; then
        grader_misconfigured "cppcheck_check execution error: $cppcheck_out"
    elif [[ "$cppcheck_rc" -ne 0 ]]; then
        echo "FAIL: cppcheck static analysis blocking findings:" >&2
        echo "$cppcheck_out" >&2
        return 1
    fi
    return 0
}
