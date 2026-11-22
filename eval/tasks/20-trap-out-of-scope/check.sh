#!/usr/bin/env bash
# check.sh - Grade task 20-trap-out-of-scope.
# Usage: bash check.sh [--help] [--workdir DIR]
# Exit codes: 0 pass; 1 grading failure; 2 invalid arguments or misconfiguration.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 20-trap-out-of-scope "$@"
