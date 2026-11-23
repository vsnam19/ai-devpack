#!/usr/bin/env bash
# check.sh - Grade a requirements-analysis candidate for can-watchdog.
#
# Usage: bash check.sh [options] CANDIDATE_DIR
# Exit codes: 0 candidate passed; 1 candidate failed; 2 invalid arguments or missing source.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
if [[ $# -eq 0 ]]; then
  exit 2
fi

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 "$TESTS_DIR/grade_findings.py" --case can-watchdog "${@: -1}"
