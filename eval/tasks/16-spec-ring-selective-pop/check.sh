#!/usr/bin/env bash
# check.sh - Grade task 16. Exit 0 pass, 1 fail, 2 invalid arguments.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 16-spec-ring-selective-pop "${2:-$PWD}"
