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
default_root=$(realpath -m -- "$HOME/.local/share/devpack")
[[ "$prefix" != / && "$prefix" != // ]] || die "refusing the root directory as prefix"
[[ ! -L "$prefix" ]] || die "prefix is a symlink: $prefix"
[[ "$launcher" != "$prefix" && "$launcher" != "$prefix/"* ]] ||
    die "put --bin-dir outside the installation prefix"

same_link() { [[ -L "$1" && $(readlink -- "$1") == "$2" ]]; }
managed() { [[ -d "$1" && ! -L "$1" && -f "$1/$manifest" && ! -L "$1/$manifest" ]]; }

clean_dir_caches() {
    local dir=$1
    [[ -d "$dir" && ! -L "$dir" ]] || return 0
    if [[ -d "$dir/__pycache__" && ! -L "$dir/__pycache__" ]]; then
        rm -rf -- "$dir/__pycache__" 2>/dev/null || return 1
        [[ ! -e "$dir/__pycache__" ]] || return 1
    fi
    find "$dir" -maxdepth 1 -type f -name '*.pyc' -delete 2>/dev/null || return 1
}

managed_launcher_link() {
    local link=$1 target target_prefix resolved_prefix parent_dir
    [[ -L "$link" ]] || return 1
    same_link "$link" "$entry" && return 0
    target=$(readlink -- "$link")
    [[ "$target" == /* ]] || target=$(realpath -m -s -- "$(dirname -- "$link")/$target")
    parent_dir=$(dirname -- "$prefix")
    if [[ "$target" == */runner/devpack_cli.py ]]; then
        target_prefix=${target%/runner/devpack_cli.py}
        if resolved_prefix=$(realpath -e -- "$target_prefix" 2>/dev/null) && managed "$resolved_prefix"; then
            return 0
        fi
        if [[ ! -e "$target_prefix" ]]; then
            return 0
        fi
    fi
    if [[ ! -e "$link" ]] && { [[ "$target" == "$default_root/"* || "$target" == "$parent_dir/"* ]]; }; then
        return 0
    fi
    return 1
}

if (( uninstall )); then
    if [[ ! -e "$prefix" ]]; then
        printf 'No installation at %s\n' "$prefix"
        exit 0
    fi
    managed "$prefix" || die "not an installer-managed prefix: $prefix"
    cleanup_failed=0
    while IFS= read -r -d '' item; do
        [[ "$item" == ./* && "$item" != */../* && "$item" != */.. ]] ||
            die "invalid installed file path: $item"
        path="$prefix/${item#./}"
        parent=$(dirname -- "$path")
        # A local edit may have replaced a directory with a symlink.
        [[ $(cd -P -- "$parent" 2>/dev/null && pwd) == "$parent" ]] || continue
        if [[ -d "$path" && ! -L "$path" ]]; then
            clean_dir_caches "$path" || cleanup_failed=1
            rmdir -- "$path" 2>/dev/null || true
        else
            rm -f -- "$path" 2>/dev/null || cleanup_failed=1
        fi
    done < "$prefix/$manifest"
    clean_dir_caches "$prefix" || cleanup_failed=1
    if (( cleanup_failed )); then
        die "failed to remove installed files in $prefix"
    fi
    while IFS= read -r -d '' item; do
        path="$prefix/${item#./}"
        if [[ -e "$path" || -L "$path" ]]; then
            if [[ ! -d "$path" || -L "$path" ]]; then
                die "failed to remove installed file: $path"
            fi
            if [[ -e "$path/__pycache__" ]]; then
                die "failed to remove cache directory: $path/__pycache__"
            fi
        fi
    done < "$prefix/$manifest"
    rm -- "$prefix/$manifest"
    rmdir -- "$prefix" 2>/dev/null || true
    if [[ -e "$prefix/__pycache__" ]]; then
        die "failed to remove cache directory: $prefix/__pycache__"
    fi
    if [[ -z "$current" ]]; then
        if same_link "$launcher" "$entry"; then rm -- "$launcher"; fi
    elif same_link "$current" "$prefix"; then
        replacement=
        parent_dir=$(dirname -- "$prefix")
        if [[ -d "$parent_dir" ]]; then
            while IFS= read -r candidate; do
                [[ -n "$candidate" && "$candidate" != "$prefix" && "$candidate" != "$current" ]] || continue
                if managed "$candidate"; then
                    replacement=$candidate
                fi
            done < <(find "$parent_dir" -mindepth 1 -maxdepth 1 -type d | sort -V)
        fi
        if [[ -n "$replacement" ]]; then
            ln -sfnT -- "$replacement" "$current"
            if same_link "$launcher" "$prefix/runner/devpack_cli.py"; then
                ln -sfnT -- "$entry" "$launcher"
            fi
        else
            if same_link "$launcher" "$entry" || same_link "$launcher" "$prefix/runner/devpack_cli.py"; then
                rm -- "$launcher"
            fi
            rm -- "$current"
        fi
    fi
    printf 'Uninstalled %s\n' "$prefix"
    exit 0
fi

[[ "$prefix" != "$source_dir" && "$prefix" != "$source_dir/"* ]] ||
    die "put --prefix outside the source directory"
if [[ -n "$checksum" ]]; then
    [[ -f "$checksum" ]] || die "checksum file not found: $checksum"
    checksum_dir=$(cd -- "$(dirname -- "$checksum")" && pwd)
    checksum_name=$(basename -- "$checksum")
    [[ "$checksum_name" == *.sha256 ]] || die "expected a .sha256 file"
    # The release checksum covers the sibling tarball.
    expected=
    archive=
    read -r expected archive < "$checksum_dir/$checksum_name" || [[ -n "$archive" ]] ||
        die "invalid release checksum file"
    [[ "$expected" =~ ^[[:xdigit:]]{64}$ && "$archive" == "${checksum_name%.sha256}.tar.gz" && "$archive" == "devpack-$version.tar.gz" ]] ||
        die "invalid release checksum file"
    (cd -- "$checksum_dir" && printf '%s  %s\n' "$expected" "$archive" | sha256sum -c -- -)
fi
command -v python3 >/dev/null || die "Python 3.10 or newer is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    die "Python 3.10 or newer is required"

if [[ -e "$launcher" || -L "$launcher" ]]; then
    managed_launcher_link "$launcher" || die "command already exists: $launcher"
fi
if [[ -n "$current" && ( -e "$current" || -L "$current" ) ]]; then
    [[ -L "$current" ]] || die "current path already exists: $current"
    previous=$(readlink -- "$current")
    [[ "$previous" == /* ]] || previous="$(dirname -- "$current")/$previous"
    previous=$(realpath -m -s -- "$previous")
    if [[ $(dirname -- "$previous") != "$(dirname -- "$prefix")" ]] || { [[ -e "$previous" || -L "$previous" ]] && ! managed "$previous"; }; then
        die "current link does not point to a managed version"
    fi
fi

if [[ -e "$prefix" ]]; then
    managed "$prefix" && [[ $(< "$prefix/VERSION") == "$version" ]] ||
        die "prefix already exists and does not contain this managed version: $prefix"
fi
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

if (( selftest )); then
    command -v shellcheck >/dev/null || die "ShellCheck is required for the self-test"
    test_state=$(mktemp -d)
    (
        trap 'rm -rf -- "$test_state"' EXIT
        PYTHONDONTWRITEBYTECODE=1 XDG_STATE_HOME="$test_state" bash "$stage/tests/run_all.sh"
    )
fi
(cd -- "$stage" && find . -depth -mindepth 1 ! -path "./$manifest" -print0) > "$stage/$manifest"
if [[ -e "$prefix" ]]; then
    find "$prefix" -type d -name __pycache__ -prune -exec rm -rf -- {} +
    cp -a --remove-destination -- "$stage/." "$prefix/"
    rm -rf -- "$stage"
else
    mv -T -n -- "$stage" "$prefix"
    [[ ! -e "$stage" ]] || die "prefix appeared during installation: $prefix"
fi
trap - EXIT
if [[ -n "$current" ]] && ! same_link "$current" "$prefix"; then
    ln -sfnT -- "$prefix" "$current"
fi
mkdir -p -- "$bin_dir"
if ! same_link "$launcher" "$entry"; then
    ln -sfnT -- "$entry" "$launcher"
fi
printf 'Installed devpack %s at %s\nCommand: %s\n' "$version" "$prefix" "$launcher"
printf 'Add %q to PATH if needed. From your project, run devpack doctor and devpack init.\n' "$bin_dir"
