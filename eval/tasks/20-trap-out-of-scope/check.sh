#!/usr/bin/env bash
# check.sh - Grade task 20. Exit 0 pass, 1 fail, 2 invalid arguments.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 20-trap-out-of-scope "${2:-$PWD}"
