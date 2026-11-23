#!/usr/bin/env bash
# Check error-status-optional candidate: preflight lint, -fno-exceptions -fno-rtti compile, and hidden tests.
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

if [[ ! -f "$CANDIDATE/voltage_decoder.hpp" || ! -f "$CANDIDATE/voltage_decoder.cpp" ]]; then
  echo "FAIL: missing voltage_decoder.hpp or voltage_decoder.cpp" >&2
  exit 1
fi

python3 "$DEVPACK_ROOT/enforcement/lint/preflight_lint.py" \
  --config "$DEVPACK_ROOT/eval/fixture/preflight.toml" \
  --repo-root "$CANDIDATE" \
  "$CANDIDATE/voltage_decoder.hpp" \
  "$CANDIDATE/voltage_decoder.cpp"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti \
  -I"$CANDIDATE" \
  "$CANDIDATE/voltage_decoder.cpp" \
  "$GRADER_DIR/grader_test.cpp" \
  -o "$tmpdir/test_voltage_decoder"

"$tmpdir/test_voltage_decoder"
