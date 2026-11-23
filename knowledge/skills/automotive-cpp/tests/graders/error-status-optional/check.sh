#!/usr/bin/env bash
# Check error-status-optional candidate: forbidden macro/pragma scan, preflight lint, -fno-exceptions -fno-rtti compile with --coverage on grader TU, and gcov out-of-process proof.
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

python3 - "$CANDIDATE/voltage_decoder.hpp" "$CANDIDATE/voltage_decoder.cpp" <<'PY'
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
  "$CANDIDATE/voltage_decoder.hpp" \
  "$CANDIDATE/voltage_decoder.cpp"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
covdir="$tmpdir/cov"
mkdir -p "$covdir"

if ! g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti \
  -I"$CANDIDATE" -c "$CANDIDATE/voltage_decoder.cpp" -o "$tmpdir/voltage_decoder.o"; then
  echo "FAIL: candidate does not compile" >&2
  exit 1
fi

if ! g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti --coverage \
  -I"$CANDIDATE" -c "$GRADER_DIR/grader_test.cpp" -o "$covdir/grader_test.o"; then
  echo "FAIL: candidate does not compile with the grader" >&2
  exit 1
fi

if ! g++ --coverage "$tmpdir/voltage_decoder.o" "$covdir/grader_test.o" -o "$tmpdir/test_voltage_decoder"; then
  echo "FAIL: candidate fails to link with the grader" >&2
  exit 1
fi

set +e
python3 "$DEVPACK_ROOT/enforcement/checks/test_proof.py" run \
  --test-src "$GRADER_DIR/grader_test.cpp" \
  --obj-dir "$covdir" \
  --assert-regex '\bGRADER_CHECK\s*\(' \
  --label "error-status-optional" \
  -- "$tmpdir/test_voltage_decoder"
proof_rc=$?
set -e
if [[ "$proof_rc" -eq 3 ]]; then
  echo "GRADER_MISCONFIGURED: gcov TOOL_MISSING" >&2
  exit 2
elif [[ "$proof_rc" -ne 0 ]]; then
  echo "FAIL: grader assertions failed" >&2
  exit 1
fi
