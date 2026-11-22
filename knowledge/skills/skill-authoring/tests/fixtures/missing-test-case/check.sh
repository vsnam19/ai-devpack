#!/usr/bin/env bash
# check.sh - require a behaviour case. Exit 0 pass, 1 fail, 2 bad usage.
set -euo pipefail
[[ "${1:-}" != "--help" ]] || { echo 'usage: check.sh TARGET'; exit 0; }
[[ $# -eq 1 ]] || exit 2
grep -q '^\[\[behaviour\]\]' "$1/output.txt"
