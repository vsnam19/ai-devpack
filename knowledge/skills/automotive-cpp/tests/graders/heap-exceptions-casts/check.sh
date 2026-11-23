#!/usr/bin/env bash
# Check heap-exceptions-casts candidate: preflight lint, -fno-exceptions -fno-rtti compile, and hidden tests.
# Exit codes: 0 pass, 1 candidate failure, 2 invalid arguments or environment.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  echo 'Usage: bash check.sh [options] CANDIDATE'
  exit 0
fi
if [[ $# -eq 0 ]]; then
  exit 2
fi
: "${DEVPACK_ROOT:?DEVPACK_ROOT must be set}"

GRADER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANDIDATE="$(cd "${@: -1}" && pwd)"

if [[ ! -f "$CANDIDATE/sample_filter.hpp" || ! -f "$CANDIDATE/sample_filter.cpp" ]]; then
  echo "FAIL: missing sample_filter.hpp or sample_filter.cpp" >&2
  exit 1
fi

python3 "$DEVPACK_ROOT/enforcement/lint/preflight_lint.py" \
  --config "$DEVPACK_ROOT/eval/fixture/preflight.toml" \
  --repo-root "$CANDIDATE" \
  "$CANDIDATE/sample_filter.hpp" \
  "$CANDIDATE/sample_filter.cpp"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti \
  -I"$CANDIDATE" \
  "$CANDIDATE/sample_filter.cpp" \
  "$GRADER_DIR/grader_test.cpp" \
  -o "$tmpdir/test_sample_filter"

"$tmpdir/test_sample_filter"
