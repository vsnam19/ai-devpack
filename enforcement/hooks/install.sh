#!/usr/bin/env bash
# Header: Installer for git enforcement hooks (pre-commit).
#
# Usage:
#   bash enforcement/hooks/install.sh [--help] [--repo PATH]
#
# Exit codes:
#   0 - Hook installed into the target git repository.
#   1 - Invalid arguments or target directory is not a git repository.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [--help] [--repo PATH]

Install the enforcement pre-commit hook into a git repository.

Options:
  --repo PATH   Target git repository root (default: current directory).
  -h, --help    Show this help message and exit.

Exit codes:
  0  Pre-commit hook installed.
  1  Target is not a valid git repository or arguments are invalid.
EOF
}

REPO_PATH="."

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --repo)
      if [ "$#" -lt 2 ]; then
        echo "INSTALL_ERROR: --repo requires a path argument" >&2
        exit 1
      fi
      REPO_PATH="$2"
      shift 2
      ;;
    *)
      echo "INSTALL_ERROR: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_HOOK="${SCRIPT_DIR}/pre-commit"

if [ ! -f "${SOURCE_HOOK}" ]; then
  echo "INSTALL_ERROR: source hook not found at ${SOURCE_HOOK}" >&2
  exit 1
fi

if ! git -C "${REPO_PATH}" rev-parse --git-dir >/dev/null 2>&1; then
  echo "INSTALL_ERROR: '${REPO_PATH}' is not a git repository" >&2
  exit 1
fi

REPO_ROOT="$(cd "${REPO_PATH}" && pwd)"
HOOKS_REL="$(git -C "${REPO_ROOT}" rev-parse --git-path hooks)"
case "${HOOKS_REL}" in
  /*) HOOKS_DIR="${HOOKS_REL}" ;;
  *)  HOOKS_DIR="${REPO_ROOT}/${HOOKS_REL}" ;;
esac

mkdir -p "${HOOKS_DIR}"
chmod +x "${SOURCE_HOOK}"
ln -sf "${SOURCE_HOOK}" "${HOOKS_DIR}/pre-commit"
echo "INSTALLED: pre-commit hook -> ${HOOKS_DIR}/pre-commit"
