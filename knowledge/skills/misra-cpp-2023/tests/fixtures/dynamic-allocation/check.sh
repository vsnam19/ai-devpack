#!/usr/bin/env bash
# check.sh - reject dynamic allocation. Exit 0 pass, 1 fail, 2 bad usage.
set -euo pipefail
[[ "${1:-}" != "--help" ]] || { echo 'usage: check.sh TARGET'; exit 0; }
[[ $# -eq 1 ]] || exit 2
! grep -Eq '\b(new|delete|malloc|free)\b' "$1/output.txt"
