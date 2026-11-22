#!/usr/bin/env bash
# Check a Doxygen parameter-direction candidate. Exit 0 pass, 1 candidate failure, 2 missing pack root.
set -euo pipefail
if [[ "${1:-}" == "--help" ]]; then
  echo 'Usage: bash check.sh [options] CANDIDATE'
  exit 0
fi
if [[ $# -eq 0 ]]; then exit 2; fi
: "${DEVPACK_ROOT:?DEVPACK_ROOT must be set}"
python3 "$DEVPACK_ROOT/skilltest/case_check.py" doxygen-naming direction-contract "${@: -1}"
