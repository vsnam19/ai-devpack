#!/usr/bin/env bash
# run_all.sh - run every unit test of the pack and shellcheck on shell scripts.
#
# Usage: bash tests/run_all.sh [--help]
# Runs `python3 -m unittest discover` for tests, enforcement, eval, runner,
# skilltest and learning when those directories exist, and for every
# knowledge/skills/*/tests directory that contains test_*.py files.
# Runs shellcheck on *.sh if installed, else prints "SKIP shellcheck: not installed".
# Fails when a core directory containing test_*.py files reports "Ran 0 tests".
# Exit codes: 0 all steps passed; 1 at least one step failed; 2 bad usage.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
if [[ $# -gt 0 ]]; then
  echo "unknown argument: $1 (try --help)" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
status=0

for dir in tests enforcement eval runner skilltest learning; do
  if [[ -d "$dir" ]]; then
    echo "== unittest: $dir"
    log="$(mktemp)"
    discover_args=(-s "$dir" -p 'test_*.py')
    if [[ -f "$dir/__init__.py" ]]; then
      discover_args=(-s "$dir" -t . -p 'test_*.py')
    fi
    if ! python3 -m unittest discover "${discover_args[@]}" 2>&1 | tee "$log"; then
      echo "FAIL unittest: $dir" >&2
      status=1
    elif [[ -n "$(find "$dir" -name 'test_*.py' -not -path '*/.git/*' -print -quit)" ]] \
      && grep -q '^Ran 0 tests' "$log"; then
      echo "FAIL unittest: $dir has test_*.py files but ran 0 tests" >&2
      status=1
    fi
    rm -f "$log"
  else
    echo "SKIP unittest: $dir (directory not present)"
  fi
done

# Skill test suites: run each knowledge/skills/*/tests directory that has test_*.py files.
# A skill suite that exists but runs 0 tests is not an error here.
shopt -s nullglob
for skill_tests in knowledge/skills/*/tests; do
  if [[ -n "$(find "$skill_tests" -name 'test_*.py' -print -quit)" ]]; then
    echo "== unittest: $skill_tests"
    if ! python3 -m unittest discover -s "$skill_tests" -p 'test_*.py'; then
      echo "FAIL unittest: $skill_tests" >&2
      status=1
    fi
  fi
done
shopt -u nullglob

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
