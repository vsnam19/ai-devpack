#!/usr/bin/env bash
# check.sh - Grade task 18. Exit 0 pass, 1 fail, 2 invalid arguments.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 18-review-fault-recovery "${2:-$PWD}"
