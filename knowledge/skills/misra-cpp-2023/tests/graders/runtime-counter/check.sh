#!/usr/bin/env bash
# Check a runtime counter candidate. Exit 0 pass, 1 candidate failure, 2 missing pack root.
set -euo pipefail
: "${DEVPACK_ROOT:?DEVPACK_ROOT must be set}"
python3 "$DEVPACK_ROOT/skilltest/case_check.py" misra-cpp-2023 runtime-counter "$1"
