#!/usr/bin/env python3
# Header: Symbol allowlist / denylist gate on compiled worker C/C++ object files.
#
# Usage:
#   python3 enforcement/checks/symbol_check.py [--worker-obj OBJ ...] [--worker-test-obj OBJ ...]
#       [--framework-obj OBJ ...] [--worker-src SRC ...] [--worker-test-src SRC ...]
#       [--framework-src SRC ...] [-I DIR ...] [--compile-commands FILE]
#       [--framework-namespace NS] [--nm-bin BIN] [--label NAME] [--json]
#
# Runs `nm -C --defined-only` and `nm -u` (with and without -C) on each object
# compiled from worker-changed sources. Fails if a worker object:
#   (a) defines any symbol in the framework namespace or any symbol also defined
#       by a framework object, or
#   (b) references any undefined symbol matching the termination/exec/gcov/fd
#       denylist.
#
# Exit codes:
#   0 - All checked worker objects satisfy symbol rules
#   1 - Symbol violation (or compilation failure of worker source)
#   2 - Invalid CLI arguments or missing object/source file
#   3 - TOOL_MISSING (nm or compiler binary not found on PATH)
"""ELF object symbol checker using nm to prevent framework overrides and forbidden primitives."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence

DEFAULT_FRAMEWORK_NAMESPACES: tuple[str, ...] = ("can_fixture_test",)
DEFAULT_EVAL_ALLOWLIST_PATH: Path = Path(__file__).resolve().with_name("symbol_allowlist_eval.txt")
VALID_IMPORT_PROFILES: tuple[str, ...] = ("production", "eval")

# Undefined symbols that worker-authored test TUs (compiled with the forced framework header) may import,
# in addition to the eval allowlist. Exact names only: the framework header uses <cstdio> and nothing else.
# Deliberately absent: __cxa_atexit, atexit, std::ios_base::Init (static-init/static-destructor hooks), and
# any iostream symbol. Keep this list identical to the framework's stdio surface.
_EVAL_TEST_FRAMEWORK_ALLOWLIST_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"^{name}$") for name in ("fprintf", "fputs", "fflush", "stderr", "stdout")
)

_ALLOWED_TEST_WEAK_TEMPLATES: frozenset[str] = frozenset(
    {
        "recordCheck",
        "recordCheckEq",
        "recordCheckFailure",
        "counters",
        "registeredSuiteHead",
        "AutoTestSuiteRegistrar",
    }
)

_DENIED_EXACT_C_OR_STD: frozenset[str] = frozenset(
    {
        "_exit",
        "_Exit",
        "exit",
        "quick_exit",
        "abort",
        "fork",
        "vfork",
        "kill",
        "raise",
        "signal",
        "sigaction",
        "dlopen",
        "dlsym",
        "syscall",
        "ptrace",
        "setjmp",
        "_setjmp",
        "__sigsetjmp",
        "sigsetjmp",
        "longjmp",
        "siglongjmp",
        "atexit",
        "at_quick_exit",
        "freopen",
        "freopen64",
        "dup2",
        "dup3",
        "pthread_exit",
        "popen",
        "system",
    }
)

_DENIED_EXACT_QUALIFIED: frozenset[str] = frozenset(
    {
        "std::set_terminate",
        "std::terminate",
    }
)

_DENIED_EXEC_RE = re.compile(r"^exec(?:l|le|lp|v|ve|vp|vpe|veat)$|^fexecve$")
_DENIED_SPAWN_RE = re.compile(r"^posix_spawnp?$")
_NM_DEFINED_LINE_RE = re.compile(r"^\s*(?:[0-9a-fA-F]+\s+)?([A-Za-z?])\s+(.+?)\s*$")
_NM_UNDEF_LINE_RE = re.compile(r"^\s*(?:[0-9a-fA-F]+\s+)?([Uwv])\s+(.+?)\s*$")

_STDLIB_WEAK_PREFIXES: tuple[str, ...] = (
    "std::",
    "__gnu_cxx::",
    "__cxxabiv1::",
    "typeinfo for std::",
    "typeinfo name for std::",
    "vtable for std::",
    "guard variable for std::",
    "non-virtual thunk to std::",
    "_ZSt",
    "_ZNSt",
    "_ZNKSt",
)


@dataclass(frozen=True)
class SymbolViolation:
    """Single symbol allowlist or denylist violation on an object file."""

    object_path: str
    kind: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class SymbolCheckResult:
    """Result of checking worker object files with ``nm``."""

    exit_code: int
    violations: tuple[SymbolViolation, ...] = ()
    errors: tuple[str, ...] = ()
    checked_objects: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def find_nm_bin(nm_bin: str = "nm") -> str | None:
    """Return resolved path to ``nm`` executable, or None if unavailable."""
    if os.sep in nm_bin or (os.altsep and os.altsep in nm_bin):
        cand = Path(nm_bin)
        return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None
    return shutil.which(nm_bin)


def _strip_version_and_signature(sym_name: str) -> str:
    """Strip ELF symbol version suffixes and C++ parameter signatures to obtain base name."""
    cleaned = sym_name.strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if "(" in cleaned and not cleaned.startswith("("):
        # Preserve template parameter lists before function parameter '(' if possible.
        cleaned = cleaned.split("(", 1)[0].strip()
    return cleaned


def _strip_template_args(base_name: str) -> str:
    """Remove trailing ``<...>`` template argument list from a demangled symbol name."""
    idx = base_name.find("<")
    return base_name[:idx].strip() if idx >= 0 else base_name


def _is_stdlib_weak_symbol(sym_type: str, sym_name: str) -> bool:
    """Return True for compiler-generated weak/unique standard library template symbols."""
    if sym_type not in ("W", "V", "u", "n"):
        return False
    return sym_name.startswith(_STDLIB_WEAK_PREFIXES)


def _normalize_defined_key(sym_name: str) -> str:
    """Normalize a defined symbol name for framework-collision comparisons."""
    cleaned = sym_name.strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    return cleaned


def _run_nm(nm_bin: str, args: Sequence[str], obj_path: Path) -> str:
    """Execute ``nm`` on ``obj_path`` and return stdout."""
    proc = subprocess.run(
        [nm_bin, *args, str(obj_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"nm exited {proc.returncode}"
        raise RuntimeError(f"nm failed on {obj_path}: {detail}")
    return proc.stdout


def list_defined_symbols(nm_bin: str, obj_path: Path) -> list[tuple[str, str]]:
    """Return ``[(sym_type, demangled_name)]`` from ``nm -C --defined-only``."""
    out = _run_nm(nm_bin, ["-C", "--defined-only"], obj_path)
    items: list[tuple[str, str]] = []
    for line in out.splitlines():
        m = _NM_DEFINED_LINE_RE.match(line)
        if m:
            items.append((m.group(1), m.group(2)))
    return items


def list_undefined_symbols(nm_bin: str, obj_path: Path) -> list[str]:
    """Return deduplicated undefined symbol names from ``nm -u -C`` and ``nm -u``."""
    seen: set[str] = set()
    syms: list[str] = []
    for flags in (["-u", "-C"], ["-u"]):
        out = _run_nm(nm_bin, flags, obj_path)
        for line in out.splitlines():
            m = _NM_UNDEF_LINE_RE.match(line)
            if not m:
                continue
            sym = m.group(2)
            if sym not in seen:
                seen.add(sym)
                syms.append(sym)
    return syms


def list_undefined_demangled_symbols(nm_bin: str, obj_path: Path) -> list[str]:
    """Return deduplicated demangled undefined symbol names from ``nm -u -C``."""
    seen: set[str] = set()
    syms: list[str] = []
    out = _run_nm(nm_bin, ["-u", "-C"], obj_path)
    for line in out.splitlines():
        m = _NM_UNDEF_LINE_RE.match(line)
        if not m:
            continue
        sym = m.group(2)
        if sym not in seen:
            seen.add(sym)
            syms.append(sym)
    return syms


def load_symbol_allowlist_file(allowlist_path: Path) -> tuple[re.Pattern[str], ...]:
    """Load one-regex-per-line undefined symbol allowlist from ``allowlist_path``."""
    if not allowlist_path.is_file():
        raise FileNotFoundError(f"symbol allowlist file not found: {allowlist_path}")
    patterns: list[re.Pattern[str]] = []
    for line_num, raw_line in enumerate(allowlist_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            patterns.append(re.compile(stripped))
        except re.error as exc:
            raise ValueError(f"invalid regex in {allowlist_path}:{line_num}: {stripped!r}: {exc}") from exc
    return tuple(patterns)


def compile_symbol_allowlist(patterns: Sequence[str | re.Pattern[str]]) -> tuple[re.Pattern[str], ...]:
    """Compile a sequence of regex strings or compiled patterns into ``re.Pattern`` objects."""
    compiled: list[re.Pattern[str]] = []
    for item in patterns:
        if isinstance(item, re.Pattern):
            compiled.append(item)
        else:
            raw = str(item).strip()
            if not raw:
                continue
            try:
                compiled.append(re.compile(raw))
            except re.error as exc:
                raise ValueError(f"invalid symbol allowlist regex {raw!r}: {exc}") from exc
    return tuple(compiled)


def is_allowed_undefined_symbol(
    demangled_sym: str,
    allowlist: Sequence[re.Pattern[str]],
    *,
    is_test_obj: bool = False,
    include_eval_test_framework: bool = False,
) -> bool:
    """Return True if ``demangled_sym`` (minus any ELF @version suffix) matches ``allowlist``."""
    cleaned = demangled_sym.strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if any(rx.search(cleaned) is not None for rx in allowlist):
        return True
    if is_test_obj and include_eval_test_framework:
        if any(rx.search(cleaned) is not None for rx in _EVAL_TEST_FRAMEWORK_ALLOWLIST_PATTERNS):
            return True
    return False


def is_denied_undefined_symbol(sym_name: str) -> str | None:
    """Return matching denylist rule name if ``sym_name`` is a forbidden undefined reference."""
    cleaned = sym_name.strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if cleaned.startswith("__gcov_"):
        return "__gcov_*"

    base = _strip_version_and_signature(cleaned)
    base_no_tmpl = _strip_template_args(base)

    if base_no_tmpl.startswith("__gcov_"):
        return "__gcov_*"
    if base_no_tmpl in _DENIED_EXACT_QUALIFIED:
        return base_no_tmpl

    if base_no_tmpl.startswith("std::"):
        bare = base_no_tmpl[5:]
        if bare in _DENIED_EXACT_C_OR_STD:
            return base_no_tmpl
        return None

    if "::" in base_no_tmpl:
        return None

    if base_no_tmpl in _DENIED_EXACT_C_OR_STD:
        return base_no_tmpl
    if _DENIED_EXEC_RE.fullmatch(base_no_tmpl):
        return "exec*"
    if _DENIED_SPAWN_RE.fullmatch(base_no_tmpl):
        return "posix_spawn*"
    return None


def _symbol_in_namespace(sym_name: str, namespace: str) -> bool:
    """Return True if demangled ``sym_name`` belongs to C++ ``namespace``."""
    prefix = f"{namespace}::"
    return prefix in sym_name


def _is_allowed_worker_test_framework_symbol(
    sym_type: str,
    sym_name: str,
    framework_namespaces: Sequence[str],
) -> bool:
    """Return True when a worker-authored test TU symbol is a legitimate test/framework instantiation."""
    for ns in framework_namespaces:
        prefix = f"{ns}::"
        if prefix not in sym_name:
            continue
        # Test functions inside anonymous namespace and static suite registrars are local.
        if f"{prefix}(anonymous namespace)::" in sym_name and sym_type in ("t", "b", "d", "r"):
            return True
        if f"{prefix}kSuiteReg" in sym_name and sym_type in ("b", "d", "r", "t"):
            return True
        # Weak template instantiations from test_framework.hpp CHECK/REGISTER macros.
        if sym_type in ("W", "V", "u", "n"):
            after_ns = sym_name.rsplit(prefix, 1)[1]
            fn_stem = _strip_template_args(_strip_version_and_signature(after_ns))
            fn_head = fn_stem.split("::", 1)[0]
            if fn_head in _ALLOWED_TEST_WEAK_TEMPLATES:
                return True
        return False
    return True


def collect_framework_symbols(nm_bin: str, framework_objs: Sequence[Path]) -> set[str]:
    """Collect normalized non-local defined symbols from all framework objects."""
    symbols: set[str] = set()
    for obj in framework_objs:
        for sym_type, sym_name in list_defined_symbols(nm_bin, obj):
            if sym_name.startswith("__gcov"):
                continue
            if not (sym_type.isupper() or sym_type == "u"):
                continue
            if _is_stdlib_weak_symbol(sym_type, sym_name):
                continue
            symbols.add(_normalize_defined_key(sym_name))
    return symbols


def check_worker_objects(
    worker_objs: Sequence[Path],
    *,
    worker_test_objs: Sequence[Path] = (),
    framework_objs: Sequence[Path] = (),
    framework_namespaces: Sequence[str] = DEFAULT_FRAMEWORK_NAMESPACES,
    nm_bin: str = "nm",
    import_profile: str = "production",
    import_allowlist: Sequence[str | re.Pattern[str]] = (),
    import_allowlist_file: Path | None = None,
) -> SymbolCheckResult:
    """Verify worker objects against framework symbol collisions and undefined symbol rules."""
    if import_profile not in VALID_IMPORT_PROFILES:
        return SymbolCheckResult(
            exit_code=2,
            errors=(f"SYMBOL_CHECK_ERROR: invalid import profile {import_profile!r}; expected one of {VALID_IMPORT_PROFILES}",),
        )

    resolved_nm = find_nm_bin(nm_bin)
    if resolved_nm is None:
        return SymbolCheckResult(
            exit_code=3,
            errors=(f"TOOL_MISSING: nm executable '{nm_bin}' not found on PATH",),
        )

    allowlist_patterns: tuple[re.Pattern[str], ...] = ()
    try:
        compiled_inline = compile_symbol_allowlist(import_allowlist)
        if import_allowlist_file is not None:
            allowlist_patterns = (*load_symbol_allowlist_file(import_allowlist_file), *compiled_inline)
        elif compiled_inline:
            allowlist_patterns = compiled_inline
        elif import_profile == "eval":
            allowlist_patterns = load_symbol_allowlist_file(DEFAULT_EVAL_ALLOWLIST_PATH)
    except (OSError, ValueError) as exc:
        return SymbolCheckResult(exit_code=2, errors=(f"SYMBOL_CHECK_ERROR: {exc}",))

    enforce_allowlist = import_profile == "eval" or bool(allowlist_patterns)

    all_paths = [*worker_objs, *worker_test_objs, *framework_objs]
    for p in all_paths:
        if not p.is_file():
            return SymbolCheckResult(
                exit_code=2,
                errors=(f"SYMBOL_CHECK_ERROR: object file not found: {p}",),
            )

    if not worker_objs and not worker_test_objs:
        return SymbolCheckResult(exit_code=0, checked_objects=())

    try:
        fw_symbols = collect_framework_symbols(resolved_nm, framework_objs)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return SymbolCheckResult(exit_code=2, errors=(f"SYMBOL_CHECK_ERROR: {exc}",))

    violations: list[SymbolViolation] = []
    checked: list[str] = []

    def _scan_one(obj: Path, *, is_test_obj: bool) -> None:
        checked.append(str(obj))
        defined = list_defined_symbols(resolved_nm, obj)
        for sym_type, sym_name in defined:
            if sym_name.startswith("__gcov_"):
                violations.append(
                    SymbolViolation(
                        object_path=str(obj),
                        kind="defined_gcov",
                        symbol=sym_name,
                        reason=f"worker object defines gcov symbol '{sym_name}'",
                    )
                )
                continue

            in_fw_ns = any(_symbol_in_namespace(sym_name, ns) for ns in framework_namespaces)
            if in_fw_ns:
                if not is_test_obj or not _is_allowed_worker_test_framework_symbol(
                    sym_type, sym_name, framework_namespaces
                ):
                    violations.append(
                        SymbolViolation(
                            object_path=str(obj),
                            kind="framework_namespace",
                            symbol=sym_name,
                            reason=f"worker object defines symbol in framework namespace: '{sym_name}' ({sym_type})",
                        )
                    )
                    continue

            if sym_type.isupper() or sym_type == "u":
                if _is_stdlib_weak_symbol(sym_type, sym_name):
                    continue
                norm = _normalize_defined_key(sym_name)
                if norm in fw_symbols:
                    if is_test_obj and _is_allowed_worker_test_framework_symbol(
                        sym_type, sym_name, framework_namespaces
                    ):
                        continue
                    violations.append(
                        SymbolViolation(
                            object_path=str(obj),
                            kind="framework_collision",
                            symbol=sym_name,
                            reason=f"worker object defines symbol already defined by framework: '{sym_name}'",
                        )
                    )

        for undef_sym in list_undefined_symbols(resolved_nm, obj):
            rule_hit = is_denied_undefined_symbol(undef_sym)
            if rule_hit is not None:
                violations.append(
                    SymbolViolation(
                        object_path=str(obj),
                        kind="denied_undefined",
                        symbol=undef_sym,
                        reason=f"worker object references denied undefined symbol '{undef_sym}' (rule: {rule_hit})",
                    )
                )

        if enforce_allowlist:
            for demangled_sym in list_undefined_demangled_symbols(resolved_nm, obj):
                if not is_allowed_undefined_symbol(
                    demangled_sym,
                    allowlist_patterns,
                    is_test_obj=is_test_obj,
                    include_eval_test_framework=(import_profile == "eval"),
                ):
                    violations.append(
                        SymbolViolation(
                            object_path=str(obj),
                            kind="symbol_not_allowed",
                            symbol=demangled_sym,
                            reason=f"SYMBOL-NOT-ALLOWED: worker object references non-allowlisted undefined symbol '{demangled_sym}'",
                        )
                    )

    try:
        for obj in worker_objs:
            _scan_one(obj, is_test_obj=False)
        for obj in worker_test_objs:
            _scan_one(obj, is_test_obj=True)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return SymbolCheckResult(exit_code=2, errors=(f"SYMBOL_CHECK_ERROR: {exc}",))

    return SymbolCheckResult(
        exit_code=1 if violations else 0,
        violations=tuple(violations),
        checked_objects=tuple(checked),
        metadata={"import_profile": import_profile, "allowlist_enforced": enforce_allowlist},
    )


_INCLUDE_DIR_SEPARATED_FLAGS: tuple[str, ...] = ("-I", "-isystem", "-iquote")
_INCLUDE_DIR_JOINED_FLAGS: tuple[str, ...] = ("-isystem", "-iquote", "-I")


def _rebase_compile_db_path(value: str, raw_dir: str) -> str:
    """Resolve a relative include directory against the compile DB entry ``directory``.

    Used for both the separated (``-I dir``) and joined (``-Idir``) spellings so that a
    relative include means the same thing whichever form the build system emitted.
    """
    p = Path(value)
    if not p.is_absolute() and raw_dir:
        p = Path(raw_dir) / p
    return str(p)


def _load_compile_db_flags(compile_commands: Path | None, src: Path) -> list[str]:
    """Extract safe -I/-D/-std compile flags for ``src`` from ``compile_commands.json`` if present."""
    if compile_commands is None or not compile_commands.is_file():
        return []
    try:
        entries = json.loads(compile_commands.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    src_res = src.resolve()
    base_dir = compile_commands.parent
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_file = str(entry.get("file", "")).strip()
        if not raw_file:
            continue
        raw_dir = str(entry.get("directory", "")).strip()
        cand_path = Path(raw_file)
        if not cand_path.is_absolute():
            cand_path = (Path(raw_dir) if raw_dir else base_dir) / cand_path
        try:
            if cand_path.resolve() != src_res:
                continue
        except OSError:
            continue
        args = entry.get("arguments")
        if isinstance(args, list):
            tokens = [str(a) for a in args]
        else:
            import shlex
            tokens = shlex.split(str(entry.get("command", "")))
        extracted: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in _INCLUDE_DIR_SEPARATED_FLAGS and i + 1 < len(tokens):
                extracted.extend([tok, _rebase_compile_db_path(tokens[i + 1], raw_dir)])
                i += 2
                continue
            joined = next(
                (flag for flag in _INCLUDE_DIR_JOINED_FLAGS if tok.startswith(flag) and len(tok) > len(flag)),
                None,
            )
            if joined is not None:
                extracted.append(joined + _rebase_compile_db_path(tok[len(joined):], raw_dir))
            elif tok.startswith(("-D", "-U", "-std=")):
                extracted.append(tok)
            i += 1
        return extracted
    return []


def compile_source_to_object(
    src: Path,
    obj_out: Path,
    *,
    include_dirs: Sequence[Path] = (),
    compile_commands: Path | None = None,
    std: str = "c++17",
    c_std: str = "c11",
    extra_flags: Sequence[str] = (),
    timeout_s: int = 30,
) -> tuple[bool, str]:
    """Compile a single C/C++ source file to ``obj_out`` without linking."""
    is_c = src.suffix.lower() == ".c"
    compiler = shutil.which("gcc" if is_c else "g++") or shutil.which("clang" if is_c else "clang++")
    if compiler is None:
        return False, f"TOOL_MISSING: no C/C++ compiler found on PATH to compile {src.name}"

    db_flags = _load_compile_db_flags(compile_commands, src)
    cmd: list[str] = [compiler]
    if not any(f.startswith("-std=") for f in db_flags):
        cmd.append(f"-std={c_std if is_c else std}")
    for inc in include_dirs:
        if inc.is_dir():
            cmd.extend(["-I", str(inc)])
    cmd.extend(db_flags)
    cmd.extend(extra_flags)
    cmd.extend(["-c", str(src), "-o", str(obj_out)])

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"symbol_check compile timed out after {timeout_s}s for {src.name}"
    except OSError as exc:
        return False, f"symbol_check compile failed to launch for {src.name}: {exc}"

    if proc.returncode != 0:
        return False, f"failed to compile {src.name}:\n{proc.stdout.strip()}"
    return True, ""


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for ``enforcement/checks/symbol_check.py``."""
    parser = argparse.ArgumentParser(
        prog="symbol_check.py",
        description="Check worker C/C++ object files with nm for framework overrides and denied symbols.",
    )
    parser.add_argument("--worker-obj", action="append", default=[], type=Path, help="Worker object (.o) file")
    parser.add_argument(
        "--worker-test-obj",
        action="append",
        default=[],
        type=Path,
        help="Worker-authored test object (.o) file under tests/",
    )
    parser.add_argument(
        "--framework-obj",
        action="append",
        default=[],
        type=Path,
        help="Framework/harness object (.o) file whose symbols worker objects may not define",
    )
    parser.add_argument(
        "--worker-src",
        action="append",
        default=[],
        type=Path,
        help="Worker C/C++ source file to compile into a temporary object and check",
    )
    parser.add_argument(
        "--worker-test-src",
        action="append",
        default=[],
        type=Path,
        help="Worker test C/C++ source file to compile into a temporary object and check",
    )
    parser.add_argument(
        "--framework-src",
        action="append",
        default=[],
        type=Path,
        help="Framework C/C++ source file to compile into a temporary object for collision checking",
    )
    parser.add_argument("-I", "--include-dir", action="append", dest="include_dirs", default=[], type=Path)
    parser.add_argument("--compile-commands", type=Path, default=None, help="Optional compile_commands.json")
    parser.add_argument("--force-include-framework", type=Path, default=None, help="Optional test_framework.hpp to force-include for test sources")
    parser.add_argument("--std", default="c++17", help="C++ standard when compiling --*-src")
    parser.add_argument("--c-std", default="c11", help="C standard when compiling --*-src")
    parser.add_argument(
        "--framework-namespace",
        action="append",
        default=[],
        help="Framework namespace(s) forbidden in worker objects (default: can_fixture_test)",
    )
    parser.add_argument(
        "--import-profile",
        choices=VALID_IMPORT_PROFILES,
        default="production",
        help="Undefined-symbol policy profile: 'production' (denylist + optional TOML allowlist) or 'eval' (strict eval allowlist)",
    )
    parser.add_argument(
        "--import-allowlist",
        type=Path,
        default=None,
        help="Optional newline-delimited regex file for undefined symbols",
    )
    parser.add_argument("--nm-bin", default=os.environ.get("NM", "nm"), help="nm executable name or path")
    parser.add_argument("--label", default="worker_objects", help="Human-readable label for diagnostics")
    parser.add_argument("--json", action="store_true", help="Emit JSON result")
    parser.add_argument("objects", nargs="*", type=Path, help="Additional worker object (.o) files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for symbol allowlist/denylist checking."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    namespaces = tuple(args.framework_namespace) if args.framework_namespace else DEFAULT_FRAMEWORK_NAMESPACES
    worker_objs = [*args.worker_obj, *args.objects]
    worker_test_objs = list(args.worker_test_obj)
    framework_objs = list(args.framework_obj)

    with tempfile.TemporaryDirectory(prefix="devpack-symcheck-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        compile_idx = 0
        for group_label, srcs, dest_list, is_fw_or_test in (
            ("worker", args.worker_src, worker_objs, False),
            ("worker_test", args.worker_test_src, worker_test_objs, True),
            ("framework", args.framework_src, framework_objs, True),
        ):
            for src in srcs:
                if not src.is_file():
                    print(f"SYMBOL_CHECK_ERROR: source file not found: {src}", file=sys.stderr)
                    return 2
                compile_idx += 1
                obj_out = tmp_path / f"{group_label}_{compile_idx}_{src.stem}.o"
                extra: list[str] = []
                if is_fw_or_test and args.force_include_framework is not None:
                    extra.extend(["-include", str(args.force_include_framework)])
                ok, err_msg = compile_source_to_object(
                    src,
                    obj_out,
                    include_dirs=args.include_dirs,
                    compile_commands=args.compile_commands,
                    std=args.std,
                    c_std=args.c_std,
                    extra_flags=extra,
                )
                if not ok:
                    if err_msg.startswith("TOOL_MISSING:"):
                        print(err_msg, file=sys.stderr)
                        return 3
                    print(f"FAIL: {args.label} {err_msg}", file=sys.stderr)
                    return 1
                dest_list.append(obj_out)

        result = check_worker_objects(
            worker_objs,
            worker_test_objs=worker_test_objs,
            framework_objs=framework_objs,
            framework_namespaces=namespaces,
            nm_bin=args.nm_bin,
            import_profile=args.import_profile,
            import_allowlist_file=args.import_allowlist,
        )

    if args.json:
        payload = {
            "exit_code": result.exit_code,
            "checked_objects": list(result.checked_objects),
            "violations": [v.__dict__ for v in result.violations],
            "errors": list(result.errors),
        }
        print(json.dumps(payload, indent=2))
    else:
        if result.errors:
            for err in result.errors:
                print(err, file=sys.stderr)
        elif result.violations:
            for v in result.violations:
                print(f"SYMBOL_CHECK_FAIL ({args.label}): {v.object_path}: {v.reason}", file=sys.stderr)
        else:
            print(f"SYMBOL_CHECK_OK ({args.label}): {len(result.checked_objects)} object(s) verified")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
