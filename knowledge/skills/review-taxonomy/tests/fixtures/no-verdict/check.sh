#!/usr/bin/env bash
# check.sh - validate review format. Exit 0 pass, 1 fail, 2 bad usage.
set -euo pipefail
[[ "${1:-}" != "--help" ]] || { echo 'usage: check.sh TARGET'; exit 0; }
[[ $# -eq 1 ]] || exit 2
grep -q '| ID | Category |' "$1/output.txt"
tail -n 1 "$1/output.txt" | grep -Eq '^VERDICT: (APPROVE|REQUEST_CHANGES)$'
