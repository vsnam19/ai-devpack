#!/usr/bin/env bash
# run_all.sh - run every unit test of the pack and shellcheck on shell scripts.
#
# Usage: bash tests/run_all.sh [--help]
# Runs `python3 -m unittest discover` for tests, enforcement, eval, runner,
# skilltest and learning when those directories exist, and for every
# knowledge/skills/*/tests directory that contains test_*.py files.
# Runs shellcheck on *.sh if installed, else prints "SKIP shellcheck: not installed".
# Fails when a core directory containing test_*.py files reports "Ran 0 tests".
# Every failed step prints its name, exit code and the last 20 lines of its output.
# Exit codes: 0 all steps passed; 1 at least one step failed; 2 bad usage.
set -euo pipefail
# Isolate from the caller's shell: CI and DEVPACK_* settings (base ref, component selection,
# test XML, deviations, baseline SHA, grader nonces, credentials) change results between runs.
unset CI DEVPACK_BASE DEVPACK_COMPONENTS DEVPACK_TEST_XML DEVPACK_DEVIATIONS DEVPACK_GCOV_OBJ_DIR \
  DEVPACK_BASELINE_SHA DEVPACK_AGENT_BIN DEVPACK_EVAL_AGENT_BIN DEVPACK_BROKER \
  DEVPACK_CLAUDE_CREDENTIALS DEVPACK_CPPCHECK_BIN DEVPACK_DIR DEVPACK_FRAMEWORK_GUARD \
  DEVPACK_HARNESS_MARKER_DIR DEVPACK_HARNESS_MARKER_NONCE DEVPACK_HARNESS_NONCE \
  DEVPACK_HARNESS_NONCE_FILE DEVPACK_LOCAL_AUTH_TOKEN DEVPACK_LOCAL_BASE_URL \
  DEVPACK_OPERATOR_CONFIG DEVPACK_SECRET_PATTERNS DEVPACK_TEST_PROOF_DIR DEVPACK_TESTS_COMPLETED \
  DEVPACK_VENDOR_DIR
unset DEVPACK_PROJECT_CONFIG

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

# run_logged LOG CMD...: run CMD with its stdout and stderr shown live and copied to LOG.
# Sets RC to the exit code of CMD. set -e is suspended so a failing CMD does not abort the script.
run_logged() {
  local log="$1"
  shift
  set +e
  "$@" 2>&1 | tee "$log"
  RC=${PIPESTATUS[0]}
  set -e
}

# report_failure NAME RC LOG: print the step name, exit code and the last 20 lines of its output.
report_failure() {
  local name="$1" rc="$2" log="$3"
  echo "FAIL $name (exit code $rc)" >&2
  echo "--- last 20 lines of output for: $name ---" >&2
  tail -n 20 "$log" >&2
  echo "--- end of output for: $name ---" >&2
}

for dir in tests enforcement eval runner skilltest learning; do
  if [[ -d "$dir" ]]; then
    echo "== unittest: $dir"
    log="$(mktemp)"
    discover_args=(-s "$dir" -p 'test_*.py')
    if [[ -f "$dir/__init__.py" ]]; then
      discover_args=(-s "$dir" -t . -p 'test_*.py')
    fi
    scope_env=()
    if [[ "$dir" == "skilltest" ]]; then
      scope_env=(DEVPACK_PROJECT_CONFIG=none)
    fi
    run_logged "$log" env "${scope_env[@]}" python3 -m unittest discover "${discover_args[@]}"
    if [[ $RC -ne 0 ]]; then
      report_failure "unittest: $dir" "$RC" "$log"
      status=1
    elif [[ -n "$(find "$dir" -name 'test_*.py' -not -path '*/.git/*' -print -quit)" ]] \
      && grep -q '^Ran 0 tests' "$log"; then
      report_failure "unittest: $dir has test_*.py files but ran 0 tests" 0 "$log"
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
    log="$(mktemp)"
    run_logged "$log" env DEVPACK_PROJECT_CONFIG=none python3 -m unittest discover -s "$skill_tests" -p 'test_*.py'
    if [[ $RC -ne 0 ]]; then
      report_failure "unittest: $skill_tests" "$RC" "$log"
      status=1
    fi
    rm -f "$log"
  fi
done
shopt -u nullglob

if command -v shellcheck >/dev/null 2>&1; then
  echo "== shellcheck"
  mapfile -t scripts < <(find . -name '*.sh' -not -path './.git/*' -not -path './.worktrees/*' | sort)
  if [[ ${#scripts[@]} -gt 0 ]]; then
    log="$(mktemp)"
    run_logged "$log" shellcheck "${scripts[@]}"
    if [[ $RC -ne 0 ]]; then
      report_failure "shellcheck" "$RC" "$log"
      status=1
    fi
    rm -f "$log"
  fi
else
  echo "SKIP shellcheck: not installed"
fi

if [[ $status -eq 0 ]]; then echo "ALL OK"; else echo "FAILED" >&2; fi
exit "$status"
