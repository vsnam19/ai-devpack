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
    git \
        -c core.fsmonitor= \
        -c core.hooksPath=/dev/null \
        -c diff.external= \
        -c core.sshCommand= \
        -c protocol.allow=never \
        -c core.pager=cat \
        "${final_args[@]}"
}
