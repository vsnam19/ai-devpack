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

# Emit GRADER_MISCONFIGURED, record DEVPACK_HARNESS_NONCE if configured, and exit 2.
grader_misconfigured() {
    echo "GRADER_MISCONFIGURED: $*" >&2
    if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
        printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
    fi
    exit 2
}

# Enumerate changed and untracked files using scope_check.py (without --exclude-standard).
# Usage: list_changed_files WORKDIR BASE_REF
list_changed_files() {
    local workdir="$1"
    local base_ref="$2"
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local scope_py="$pack_root/enforcement/checks/scope_check.py"
    if [[ ! -f "$scope_py" ]]; then
        grader_misconfigured "missing $scope_py"
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
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE "$@" >"$tmp_out" 2>&1 || rc=$?
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

# Compile worker-changed C/C++ sources (or src/*.cpp when headers/src exist) and run symbol_check.py.
# Usage: run_worker_symbol_check WORKDIR [BASE_REF]
run_worker_symbol_check() {
    local workdir="$1"
    local base_ref="${2:-${DEVPACK_BASELINE_SHA:-}}"
    local pack_root="${DEVPACK_ROOT:-$_SAFE_GIT_DEFAULT_ROOT}"
    local sym_py="$pack_root/enforcement/checks/symbol_check.py"
    local pack_fw="$pack_root/eval/fixture/tests/test_framework.hpp"

    if [[ ! -f "$sym_py" ]]; then
        grader_misconfigured "missing symbol_check.py under DEVPACK_ROOT=$pack_root"
    fi

    local changed_list=""
    if [[ -n "$base_ref" ]] && safe_git -C "$workdir" cat-file -e "${base_ref}^{commit}" 2>/dev/null; then
        changed_list="$(list_changed_files "$workdir" "$base_ref" 2>/dev/null || true)"
    fi

    local worker_srcs=()
    local worker_test_srcs=()
    local check_all_src=0
    if [[ -z "$changed_list" ]]; then
        check_all_src=1
    else
        local rel_f
        while IFS= read -r rel_f; do
            [[ -z "$rel_f" ]] && continue
            case "$rel_f" in
                include/*.h|include/*.hh|include/*.hpp|include/*.hxx)
                    check_all_src=1
                    ;;
                src/*.c|src/*.cc|src/*.cpp|src/*.cxx)
                    if [[ -f "$workdir/$rel_f" ]]; then
                        worker_srcs+=("$workdir/$rel_f")
                    fi
                    ;;
                tests/*.c|tests/*.cc|tests/*.cpp|tests/*.cxx)
                    if [[ -f "$workdir/$rel_f" ]]; then
                        case "$(basename "$rel_f")" in
                            test_main.cpp|test_can_codec.cpp|test_ring_buffer.cpp|test_link_state_machine.cpp|test_dtc_debouncer.cpp)
                                ;;
                            *)
                                worker_test_srcs+=("$workdir/$rel_f")
                                ;;
                        esac
                    fi
                    ;;
                *.c|*.cc|*.cpp|*.cxx)
                    if [[ -f "$workdir/$rel_f" ]]; then
                        worker_srcs+=("$workdir/$rel_f")
                    fi
                    ;;
            esac
        done <<< "$changed_list"
    fi

    if [[ "$check_all_src" -eq 1 && -d "$workdir/src" ]]; then
        worker_srcs=()
        local sf
        while IFS= read -r sf; do
            worker_srcs+=("$sf")
        done < <(find "$workdir/src" -maxdepth 1 -type f \( -name '*.cpp' -o -name '*.cc' -o -name '*.c' -o -name '*.cxx' \) | sort)
    fi

    if [[ "${#worker_srcs[@]}" -eq 0 && "${#worker_test_srcs[@]}" -eq 0 ]]; then
        return 0
    fi

    local sym_args=(--label "worker_symbol_check")
    if [[ -d "$workdir/include" ]]; then
        sym_args+=(-I "$workdir/include")
    fi
    if [[ -d "$workdir/tests" ]]; then
        sym_args+=(-I "$workdir/tests")
    fi
    if [[ -f "$pack_fw" ]]; then
        sym_args+=(--force-include-framework "$pack_fw")
    fi
    if [[ -f "$workdir/tests/test_main.cpp" ]]; then
        sym_args+=(--framework-src "$workdir/tests/test_main.cpp")
    elif [[ -f "$pack_root/eval/fixture/tests/test_main.cpp" ]]; then
        sym_args+=(--framework-src "$pack_root/eval/fixture/tests/test_main.cpp")
    fi
    local s
    for s in "${worker_srcs[@]}"; do
        sym_args+=(--worker-src "$s")
    done
    for s in "${worker_test_srcs[@]}"; do
        sym_args+=(--worker-test-src "$s")
    done

    local rc=0
    python3 "$sym_py" "${sym_args[@]}" >&2 || rc=$?
    if [[ "$rc" -eq 3 ]]; then
        grader_misconfigured "nm or compiler TOOL_MISSING in symbol_check"
    elif [[ "$rc" -eq 2 ]]; then
        grader_misconfigured "symbol_check internal error"
    elif [[ "$rc" -ne 0 ]]; then
        echo "FAIL: symbol_check rejected worker object symbols" >&2
        return 1
    fi
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
    local sym_py="$pack_root/enforcement/checks/symbol_check.py"
    local pack_fw="$pack_root/eval/fixture/tests/test_framework.hpp"

    if [[ ! -f "$proof_py" || ! -f "$pack_fw" || ! -f "$sym_py" ]]; then
        grader_misconfigured "missing test_proof.py, symbol_check.py, or test_framework.hpp under DEVPACK_ROOT=$pack_root"
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

    if [[ "${#src_objs[@]}" -gt 0 ]]; then
        local sym_args=(--label "$label")
        local fw_o
        for fw_o in "${test_objs[@]}"; do
            sym_args+=(--framework-obj "$fw_o")
        done
        local w_o
        for w_o in "${src_objs[@]}"; do
            sym_args+=(--worker-obj "$w_o")
        done
        local sym_rc=0
        python3 "$sym_py" "${sym_args[@]}" >&2 || sym_rc=$?
        if [[ "$sym_rc" -eq 3 ]]; then
            rm -rf "$tmp_grade_dir"
            grader_misconfigured "nm TOOL_MISSING"
        elif [[ "$sym_rc" -eq 2 ]]; then
            rm -rf "$tmp_grade_dir"
            grader_misconfigured "symbol_check internal error"
        elif [[ "$sym_rc" -ne 0 ]]; then
            rm -rf "$tmp_grade_dir"
            return 1
        fi
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
    python3 "$proof_py" "${proof_args[@]}" || rc=$?
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
        grader_misconfigured "missing test_proof.py or fixture/tests under DEVPACK_ROOT=$pack_root"
    fi

    local tmp_vis_dir
    tmp_vis_dir="$(mktemp -d "${TMPDIR:-/tmp}/devpack-gcov-visible.XXXXXX")"
    local cov_dir="$tmp_vis_dir/cov"
    mkdir -p "$cov_dir"

    run_worker_symbol_check "$workdir" "${DEVPACK_BASELINE_SHA:-}"

    local out_file="$tmp_vis_dir/visible.out"
    local test_start_time
    test_start_time="$(python3 -c 'import time; print(time.time())')"
    local test_rc=0
    env -u DEVPACK_HARNESS_NONCE -u DEVPACK_HARNESS_NONCE_FILE \
        DEVPACK_ROOT="$pack_root" DEVPACK_GCOV_OBJ_DIR="$cov_dir" \
        bash "$workdir/build_and_test.sh" --no-cmake >"$out_file" 2>&1 || test_rc=$?

    if [[ "$test_rc" -ne 0 ]]; then
        cat "$out_file" >&2 || true
        rm -rf "$tmp_vis_dir"
        echo "FAIL: $label exited with status $test_rc" >&2
        return 1
    fi

    local proof_args=(verify --obj-dir "$cov_dir" --stdout-file "$out_file" --returncode "$test_rc" --start-time "$test_start_time" --label "$label")
    local f
    for f in "$fixture_tests_dir"/*.cpp; do
        [[ -f "$f" ]] && proof_args+=(--test-src "$f")
    done
    for f in "${extra_tests[@]}"; do
        [[ -n "$f" && -f "$f" ]] && proof_args+=(--test-src "$f")
    done

    local proof_rc=0
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
        echo "GRADER_MISCONFIGURED: missing external DEVPACK_BASELINE_SHA (commit-message lookup is disabled)" >&2
        if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
            printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
        fi
        return 2
    fi
    if ! safe_git -C "$workdir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        echo "GRADER_MISCONFIGURED: DEVPACK_BASELINE_SHA '$sha' does not resolve to a commit in $workdir" >&2
        if [[ -n "${DEVPACK_HARNESS_NONCE:-}" && -n "${DEVPACK_HARNESS_NONCE_FILE:-}" ]]; then
            printf '%s\n' "$DEVPACK_HARNESS_NONCE" > "$DEVPACK_HARNESS_NONCE_FILE" 2>/dev/null || true
        fi
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
        grader_misconfigured "missing preflight_lint.py, cppcheck_check.py, or preflight.toml under DEVPACK_ROOT=$pack_root"
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
