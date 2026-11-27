#!/usr/bin/env bash
# Install a local devpack release and publish its command.
set -euo pipefail

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'HELP'
Usage: ./install.sh [--prefix DIR] [--bin-dir DIR] [--verify FILE.sha256] [--no-selftest]
       ./install.sh --uninstall [--prefix DIR] [--bin-dir DIR]

The default prefix is $HOME/.local/share/devpack/<version>.
The default command directory is $HOME/.local/bin.
Use a checksum file and its matching release archive in the same directory.
HELP
}

source_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
prefix=
bin_dir=
checksum=
selftest=1
uninstall=0
while (( $# )); do
    case "$1" in
        --prefix|--bin-dir|--verify)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 requires a value"
            case "$1" in
                --prefix) prefix=$2 ;;
                --bin-dir) bin_dir=$2 ;;
                --verify) checksum=$2 ;;
            esac
            shift ;;
        --no-selftest) selftest=0 ;;
        --uninstall) uninstall=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift
done

version=$(< "$source_dir/VERSION")
[[ "$version" =~ ^[[:alnum:]][[:alnum:].+_-]*$ && "$version" != current ]] || die "invalid VERSION"
current=
if [[ -z "$prefix" ]]; then
    prefix="$HOME/.local/share/devpack/$version"
    current="$HOME/.local/share/devpack/current"
fi
bin_dir=${bin_dir:-"$HOME/.local/bin"}
# Resolve existing parent aliases without dereferencing the final path.
prefix=$(realpath -m -s -- "$prefix")
prefix="$(realpath -m -- "$(dirname -- "$prefix")")/$(basename -- "$prefix")"
bin_dir=$(realpath -m -- "$bin_dir")
if [[ -n "$current" ]]; then
    current="$(dirname -- "$prefix")/current"
fi
launcher="$bin_dir/devpack"
entry="${current:-$prefix}/runner/devpack_cli.py"
manifest=.devpack-installed-files
[[ "$prefix" != / && "$prefix" != // ]] || die "refusing the root directory as prefix"
[[ ! -L "$prefix" ]] || die "prefix is a symlink: $prefix"
[[ "$launcher" != "$prefix" && "$launcher" != "$prefix/"* ]] ||
    die "put --bin-dir outside the installation prefix"

same_link() { [[ -L "$1" && $(readlink -- "$1") == "$2" ]]; }
managed() { [[ -d "$1" && ! -L "$1" && -f "$1/$manifest" && ! -L "$1/$manifest" ]]; }

if (( uninstall )); then
    if [[ ! -e "$prefix" ]]; then
        printf 'No installation at %s\n' "$prefix"
        exit 0
    fi
    managed "$prefix" || die "not an installer-managed prefix: $prefix"
    if [[ -z "$current" ]] || same_link "$current" "$prefix"; then
        if same_link "$launcher" "$entry"; then rm -- "$launcher"; fi
        if [[ -n "$current" ]]; then rm -- "$current"; fi
    fi
    while IFS= read -r -d '' item; do
        [[ "$item" == ./* && "$item" != */../* && "$item" != */.. ]] ||
            die "invalid installed file path: $item"
        path="$prefix/${item#./}"
        parent=$(dirname -- "$path")
        # A local edit may have replaced a directory with a symlink.
        [[ $(cd -P -- "$parent" 2>/dev/null && pwd) == "$parent" ]] || continue
        if [[ -d "$path" && ! -L "$path" ]]; then
            rmdir -- "$path" 2>/dev/null || true
        else
            rm -f -- "$path"
        fi
    done < "$prefix/$manifest"
    rm -- "$prefix/$manifest"
    rmdir -- "$prefix" 2>/dev/null || true
    printf 'Uninstalled %s\n' "$prefix"
    exit 0
fi

[[ "$prefix" != "$source_dir" && "$prefix" != "$source_dir/"* ]] ||
    die "put --prefix outside the source directory"
if [[ -n "$checksum" ]]; then
    checksum_dir=$(cd -- "$(dirname -- "$checksum")" && pwd)
    checksum_name=$(basename -- "$checksum")
    [[ "$checksum_name" == *.sha256 ]] || die "expected a .sha256 file"
    # The release checksum covers the sibling tarball.
    read -r expected archive < "$checksum_dir/$checksum_name"
    [[ "$expected" =~ ^[[:xdigit:]]{64}$ && "$archive" == "${checksum_name%.sha256}.tar.gz" ]] ||
        die "invalid release checksum file"
    (cd -- "$checksum_dir" && printf '%s  %s\n' "$expected" "$archive" | sha256sum -c -- -)
fi
command -v python3 >/dev/null || die "Python 3.10 or newer is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    die "Python 3.10 or newer is required"

if [[ -e "$launcher" || -L "$launcher" ]]; then
    same_link "$launcher" "$entry" || die "command already exists: $launcher"
fi
if [[ -n "$current" && ( -e "$current" || -L "$current" ) ]]; then
    [[ -L "$current" ]] || die "current path already exists: $current"
    previous=$(readlink -- "$current")
    if [[ $(dirname -- "$previous") != "$(dirname -- "$prefix")" ]] || ! managed "$previous"; then
        die "current link does not point to a managed version"
    fi
fi

if [[ -e "$prefix" ]]; then
    managed "$prefix" && [[ $(< "$prefix/VERSION") == "$version" ]] ||
        die "prefix already exists and does not contain this managed version: $prefix"
    stage="$prefix"
else
    mkdir -p -- "$(dirname -- "$prefix")"
    stage=$(mktemp -d -- "$(dirname -- "$prefix")/.install.XXXXXXXX")
    trap 'rm -rf -- "$stage"' EXIT
    # Include the release payload and omit checkout metadata and generated files.
    for item in VERSION install.sh LICENSE README.md CHANGELOG.md .devpack .gitignore \
        docs enforcement eval knowledge learning orchestrator policy runner skilltest tests tools workflow; do
        if [[ -e "$source_dir/$item" ]]; then
            cp -a -- "$source_dir/$item" "$stage/"
        fi
    done
    find "$stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
    chmod +x -- "$stage/runner/devpack_cli.py"
fi

if (( selftest )); then
    command -v shellcheck >/dev/null || die "ShellCheck is required for the self-test"
    test_state=$(mktemp -d)
    (
        trap 'rm -rf -- "$test_state"' EXIT
        PYTHONDONTWRITEBYTECODE=1 XDG_STATE_HOME="$test_state" bash "$stage/tests/run_all.sh"
    )
fi
if [[ "$stage" != "$prefix" ]]; then
    (cd -- "$stage" && find . -depth -mindepth 1 ! -path "./$manifest" -print0) > "$stage/$manifest"
    mv -T -n -- "$stage" "$prefix"
    [[ ! -e "$stage" ]] || die "prefix appeared during installation: $prefix"
    trap - EXIT
fi
if [[ -n "$current" ]] && ! same_link "$current" "$prefix"; then
    ln -sfnT -- "$prefix" "$current"
fi
mkdir -p -- "$bin_dir"
if ! same_link "$launcher" "$entry"; then
    ln -sT -- "$entry" "$launcher"
fi
printf 'Installed devpack %s at %s\nCommand: %s\n' "$version" "$prefix" "$launcher"
printf 'Add %q to PATH if needed. From your project, run devpack doctor and devpack init.\n' "$bin_dir"
