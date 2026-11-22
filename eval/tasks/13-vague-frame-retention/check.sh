#!/usr/bin/env bash
# check.sh - Grade task 13. Exit 0 pass, 1 fail, 2 invalid arguments.
set -euo pipefail
exec bash "$(dirname "$0")/../v2_check.sh" 13-vague-frame-retention "${2:-$PWD}"
