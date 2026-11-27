#!/usr/bin/env bash
# Build and verify a tagged source release, or smoke-test the committed HEAD.
set -euo pipefail

if [[ $# -ne 2 || ! "$1" =~ ^[[:alnum:]][[:alnum:].+_-]*$ || -z "$2" ]]; then
    printf 'Usage: %s VERSION OUTDIR\n' "$0" >&2
    exit 2
fi

if ! command -v shellcheck >/dev/null 2>&1; then
    printf 'Release verification requires shellcheck on PATH.\n' >&2
    exit 1
fi

version=${1#v}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
name="devpack-$version"

# Ignore caller-selected repositories, config overrides, and replacement objects.
for variable in "${!GIT_@}"; do
    unset "$variable"
done
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_ATTR_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1

release_git() {
    git --no-replace-objects -c core.attributesFile=/dev/null "$@"
}

if [[ "$version" == 0.0.0-test ]]; then
    ref=HEAD
elif release_git -C "$root" show-ref --verify --quiet "refs/tags/$version"; then
    ref="refs/tags/$version"
elif release_git -C "$root" show-ref --verify --quiet "refs/tags/v$version"; then
    ref="refs/tags/v$version"
else
    printf 'No release tag found for version %s.\n' "$version" >&2
    exit 1
fi
commit=$(release_git -C "$root" rev-parse --verify "$ref^{commit}")

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

# Share only the object store so local archive config and info/attributes cannot affect output.
objects=$(release_git -C "$root" rev-parse --path-format=absolute --git-path objects)
object_format=$(release_git -C "$root" rev-parse --show-object-format)
release_git init -q --bare --template= --object-format="$object_format" "$tmp/archive.git"
printf '%s\n' "$objects" > "$tmp/archive.git/objects/info/alternates"

# Exclude generated files even if they were committed in the selected revision.
release_git -C "$tmp/archive.git" archive --format=tar --prefix="$name/" "$commit" -- \
    . ':(exclude,glob)**/.git' ':(exclude,glob)**/.git/**' \
    ':(exclude,glob)**/.worktrees/**' ':(exclude,glob)**/runs/**' \
    ':(exclude,glob)**/__pycache__/**' ':(exclude,glob)**/*.pyc' \
    ':(exclude,icase,glob)**/*receipt*.json' ':(exclude,icase,glob)**/*receipt*.jsonl' \
    ':(exclude,glob)**/evidence/*.json' ':(exclude)skilltest/results' ':(exclude)eval/results' \
    ':(exclude)learning/reports' | gzip -n > "$tmp/$name.tar.gz"

tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
(
    cd -- "$tmp/$name"
    export PYTHONDONTWRITEBYTECODE=1
    export XDG_STATE_HOME="$tmp/state"
    if [[ ! -f VERSION || -L VERSION ]]; then
        printf 'Archive must contain a regular VERSION file.\n' >&2
        exit 1
    fi
    if [[ "$version" != 0.0.0-test && $(< VERSION) != "$version" ]]; then
        printf 'Archived VERSION does not match requested version %s.\n' "$version" >&2
        exit 1
    fi
    if [[ ! -f install.sh || -L install.sh || ! -x install.sh ]]; then
        printf 'Archive must contain an executable install.sh at its root.\n' >&2
        exit 1
    fi
    python3 "$root/tests/test_no_project_leaks.py" --scan-tree "$tmp/$name"

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
home = re.compile(
    r"/(?:ho" r"me|Users)/(?!(?:example|user|you)(?![\w.-]))"
    r"[\w.-]+(?![\w.-])"
)
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
mkdir -p -- "$2"
outdir=$(cd -- "$2" && pwd)
mv -- "$tmp/$name.tar.gz" "$tmp/$name.sha256" "$outdir/"
printf 'RELEASE_OK %s\n' "$checksum"
