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

# Execute a test command/binary and require exit 0 + valid DEVPACK_TESTS_COMPLETED proof.
# Usage: run_verified_test_binary LABEL EXPECTED_MIN CMD [ARGS...]
run_verified_test_binary() {
    local label="$1"
    local expected_min="$2"
    shift 2
    local tmp_out
    tmp_out="$(mktemp "${TMPDIR:-/tmp}/devpack-test-proof.XXXXXX")"
    local rc=0
    "$@" >"$tmp_out" 2>&1 || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        cat "$tmp_out" >&2 || true
        rm -f "$tmp_out"
        echo "FAIL: $label exited with status $rc" >&2
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
        return 2
    fi
    if ! safe_git -C "$workdir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        echo "GRADER_MISCONFIGURED: DEVPACK_BASELINE_SHA '$sha' does not resolve to a commit in $workdir" >&2
        return 2
    fi
    echo "$sha"
}
