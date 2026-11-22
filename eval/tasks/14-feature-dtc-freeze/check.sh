#!/usr/bin/env bash
# check.sh - Grade task 14. Exit 0 pass, 1 fail, 2 invalid arguments.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 14-feature-dtc-freeze "${2:-$PWD}"
