#!/usr/bin/env bash
# run_all.sh - run every unit test of the pack and shellcheck on shell scripts.
#
# Usage: bash tests/run_all.sh [--help]
# Runs `python3 -m unittest discover` for tests, enforcement, eval and runner when those
# directories exist. Runs shellcheck on *.sh if installed, else prints
# "SKIP shellcheck: not installed".
# Exit codes: 0 all steps passed; 1 at least one step failed; 2 bad usage.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
if [[ $# -gt 0 ]]; then
  echo "unknown argument: $1 (try --help)" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
status=0

for dir in tests enforcement eval runner; do
  if [[ -d "$dir" ]]; then
    echo "== unittest: $dir"
    if ! python3 -m unittest discover -s "$dir" -p 'test_*.py'; then
      echo "FAIL unittest: $dir" >&2
      status=1
    fi
  else
    echo "SKIP unittest: $dir (directory not present)"
  fi
done

if command -v shellcheck >/dev/null 2>&1; then
  echo "== shellcheck"
  mapfile -t scripts < <(find . -name '*.sh' -not -path './.git/*' -not -path './.worktrees/*' | sort)
  if [[ ${#scripts[@]} -gt 0 ]]; then
    if ! shellcheck "${scripts[@]}"; then
      echo "FAIL shellcheck" >&2
      status=1
    fi
  fi
else
  echo "SKIP shellcheck: not installed"
fi

if [[ $status -eq 0 ]]; then echo "ALL OK"; else echo "FAILED" >&2; fi
exit "$status"
