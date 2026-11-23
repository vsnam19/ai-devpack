#!/usr/bin/env bash
# Check doxygen-directions candidate: preflight lint, forbidden macro/pragma scan, required Doxygen tags, compile with --coverage on grader TU, and gcov out-of-process proof.
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

python3 - "$CANDIDATE/frame_crc.hpp" "$CANDIDATE/frame_crc.cpp" <<'PY'
import pathlib, re, sys
patterns = [
    (r"\bNDEBUG\b", "forbidden NDEBUG reference"),
    (r"#\s*pragma\s+(?:GCC|clang)\s+diagnostic\b", "forbidden compiler diagnostic pragma"),
    (r"\b_Pragma\s*\(", "forbidden _Pragma directive"),
    (r"#\s*(?:undef|define)\s+GRADER_[A-Za-z0-9_]*\b", "forbidden grader macro manipulation"),
]
for raw in sys.argv[1:]:
    text = pathlib.Path(raw).read_text(encoding="utf-8")
    for rx, msg in patterns:
        if re.search(rx, text):
            print(f"FAIL: {msg} in {pathlib.Path(raw).name}", file=sys.stderr)
            sys.exit(1)
PY

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
covdir="$tmpdir/cov"
mkdir -p "$covdir"

if ! g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti \
  -I"$CANDIDATE" -c "$CANDIDATE/frame_crc.cpp" -o "$tmpdir/frame_crc.o"; then
  echo "FAIL: candidate does not compile" >&2
  exit 1
fi

if ! g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti --coverage \
  -I"$CANDIDATE" -c "$GRADER_DIR/grader_test.cpp" -o "$covdir/grader_test.o"; then
  echo "FAIL: candidate does not compile with the grader" >&2
  exit 1
fi

if ! g++ --coverage "$tmpdir/frame_crc.o" "$covdir/grader_test.o" -o "$tmpdir/test_frame_crc"; then
  echo "FAIL: candidate fails to link with the grader" >&2
  exit 1
fi

set +e
python3 "$DEVPACK_ROOT/enforcement/checks/test_proof.py" run \
  --test-src "$GRADER_DIR/grader_test.cpp" \
  --obj-dir "$covdir" \
  --assert-regex '\bGRADER_CHECK\s*\(' \
  --label "doxygen-directions" \
  -- "$tmpdir/test_frame_crc"
proof_rc=$?
set -e
if [[ "$proof_rc" -eq 3 ]]; then
  echo "GRADER_MISCONFIGURED: gcov TOOL_MISSING" >&2
  exit 2
elif [[ "$proof_rc" -ne 0 ]]; then
  echo "FAIL: grader known-answer assertions failed" >&2
  exit 1
fi
