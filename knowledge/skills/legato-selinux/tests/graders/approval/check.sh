#!/usr/bin/env bash
set -euo pipefail
candidates=("${@: -1}")
candidate="${candidates[0]}"
python3 - "$candidate" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
questions = root / 'QUESTIONS.md'
text = questions.read_text() if questions.exists() else ''
assert all(word in text for word in ('unknown', 'SDK', 'Legato', 'selinux-manifest')), 'Missing questions/approval'
assert not any(root.rglob('*.te')), 'Policy generated before approval'
PY
