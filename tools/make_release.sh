#!/usr/bin/env bash
# Build and verify a source release from a version tag, or from HEAD if absent.
set -euo pipefail

if [[ $# -ne 2 || ! "$1" =~ ^[[:alnum:]][[:alnum:].+_-]*$ || -z "$2" ]]; then
    printf 'Usage: %s VERSION OUTDIR\n' "$0" >&2
    exit 2
fi

version=$1
mkdir -p -- "$2"
outdir=$(cd -- "$2" && pwd)
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
name="devpack-$version"
tag="refs/tags/$version"

ref=HEAD
if git -C "$root" show-ref --verify --quiet "$tag"; then
    ref=$tag
elif git -C "$root" show-ref --verify --quiet "refs/tags/v$version"; then
    ref="refs/tags/v$version"
fi

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

# Exclude generated files even if they were committed in the selected revision.
git -C "$root" archive --format=tar --prefix="$name/" "$ref" -- \
    . ':(exclude,glob)**/.git' ':(exclude,glob)**/.git/**' \
    ':(exclude,glob)**/.worktrees/**' ':(exclude,glob)**/runs/**' \
    ':(exclude,glob)**/__pycache__/**' ':(exclude,glob)**/*.pyc' \
    ':(exclude,icase,glob)**/*receipt*.json' ':(exclude,icase,glob)**/*receipt*.jsonl' \
    ':(exclude,glob)evidence/*.json' ':(exclude)skilltest/results' \
    ':(exclude)learning/reports' | gzip -n > "$tmp/$name.tar.gz"

tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
(
    cd -- "$tmp/$name"
    export PYTHONDONTWRITEBYTECODE=1
    export XDG_STATE_HOME="$tmp/state"
    python3 tests/test_no_project_leaks.py

    python3 - <<'PY'
import re
import sys
from pathlib import Path

# Assemble signatures so this scanner can scan its own source.
assistant_names = "|".join((
    "Clau" + "de", "Anthro" + "pic", "Co" + "dex", "Open" + "AI",
    "Chat" + "GPT", "Co" + "pilot", "Gemi" + "ni", "Cursor",
))
attribution = re.compile(
    r"^[ \t]*(?:[\w]+(?:-[\w]+)+|Author):[^\n]*\b(?:"
    + assistant_names + r")\b"
    + "|" + "Clau" + "de-Session"
    + "|" + "noreply@" + "anthropic"
    + "|" + r"Generated\s+with\s+\[Clau" + "de",
    re.IGNORECASE | re.MULTILINE,
)
home = re.compile(r"/(?:ho" r"me|Users)/(?!example/|user/|you/)[\w.-]+/")
failed = False

for path in sorted(Path(".").rglob("*")):
    inputs = [(str(path), "path")]
    if path.is_symlink():
        inputs.append((str(path.readlink()), "link target"))
    elif path.is_file():
        inputs.append((path.read_bytes().decode("utf-8", errors="ignore"), "content"))
    for value, location in inputs:
        for pattern, label in ((attribution, "AI attribution"), (home, "absolute home path")):
            match = pattern.search(value)
            if match:
                line = value.count("\n", 0, match.start()) + 1
                print(f"{path}:{line}: {label} in {location}", file=sys.stderr)
                failed = True

sys.exit(int(failed))
PY

    bash tests/run_all.sh
)

(
    cd -- "$tmp"
    sha256sum "$name.tar.gz" > "$name.sha256"
)
read -r checksum _ < "$tmp/$name.sha256"
mv -- "$tmp/$name.tar.gz" "$tmp/$name.sha256" "$outdir/"
printf 'RELEASE_OK %s\n' "$checksum"
