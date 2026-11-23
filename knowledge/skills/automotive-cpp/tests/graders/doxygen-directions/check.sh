#!/usr/bin/env bash
# Check doxygen-directions candidate: preflight lint, required Doxygen tags, compile, and hidden tests.
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

if [[ ! -f "$CANDIDATE/frame_crc.hpp" || ! -f "$CANDIDATE/frame_crc.cpp" ]]; then
  echo "FAIL: missing frame_crc.hpp or frame_crc.cpp" >&2
  exit 1
fi

python3 "$DEVPACK_ROOT/enforcement/lint/preflight_lint.py" \
  --config "$DEVPACK_ROOT/eval/fixture/preflight.toml" \
  --repo-root "$CANDIDATE" \
  "$CANDIDATE/frame_crc.hpp" \
  "$CANDIDATE/frame_crc.cpp"

# Verify non-void @return and [in,out] / [out] directions in header Doxygen.
grep -q '@file' "$CANDIDATE/frame_crc.hpp"
grep -q '@brief' "$CANDIDATE/frame_crc.hpp"
grep -q '@param\[in,out\].*crcAccumulator' "$CANDIDATE/frame_crc.hpp"
grep -q '@param\[out\].*isValidOut' "$CANDIDATE/frame_crc.hpp"
grep -q '@return' "$CANDIDATE/frame_crc.hpp"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti \
  -I"$CANDIDATE" \
  "$CANDIDATE/frame_crc.cpp" \
  "$GRADER_DIR/grader_test.cpp" \
  -o "$tmpdir/test_frame_crc"

"$tmpdir/test_frame_crc"
