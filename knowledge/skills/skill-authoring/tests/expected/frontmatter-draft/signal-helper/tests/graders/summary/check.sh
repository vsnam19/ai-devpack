#!/usr/bin/env bash
# Check a summary candidate. Exit 0 pass, 1 missing summary, 2 invalid usage.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
  echo 'Usage: bash check.sh [options] CANDIDATE'
  exit 0
fi
if [[ $# -eq 0 ]]; then exit 2; fi
candidate=("${@: -1}")
test -f "${candidate[0]}/SUMMARY.md"
