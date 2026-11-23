#!/usr/bin/env python3
"""
@file preflight_lint.py
@brief Generic, project-neutral C/C++ preflight linter with MISRA-style checks and baseline ratcheting.
@details Checks C and C++ source and header files for:
  - MISRA-style lexical and AST rules:
    * No C-style casts (MISRA-8.2.2)
    * No dynamic heap allocation new/delete/malloc/free outside allowed paths (MISRA-21.6.1 / MISRA-C-21.3)
    * No implicit lambda captures [=] or [&] (MISRA-LAMBDA-CAPTURE)
    * Explicit single-argument constructors (MISRA-15.1.3)
    * Overridden virtual functions must use override or final (MISRA-13.3.1)
    * Switch statements must have a default clause (MISRA-9.4.2 / MISRA-C-16.4)
    * Control-flow bodies (if/else/for/while/do) must use compound braces {} (MISRA-9.3.1 / MISRA-C-15.6)
    * No goto statements (MISRA-9.6.1 / MISRA-C-15.1)
    * No commented-out code (MISRA-5.7.2) or empty comments (MISRA-COMMENT-REDUNDANT)
    * No process-terminating/shell calls abort/exit/system (MISRA-18.5.2 / MISRA-C-21.8)
    * No setjmp/longjmp (MISRA-21.10.2 / MISRA-C-21.4)
    * No unbounded C string functions strcpy/strcat/sprintf/vsprintf/gets (MISRA-STR-BOUND / MISRA-C-21.17)
    * No macro redefinition of keywords (MISRA-C-20.4) or Variable Length Arrays (MISRA-C-18.8)
    * Optional no-exceptions (NO-EXCEPTIONS), no-rtti (NO-RTTI), no-impl-in-header (NO-IMPL-IN-HEADER)
  - Doxygen presence (@file and @brief: DOXYGEN-FILE) and @param[in|out|in,out] directions (DOXYGEN-PARAM-DIR)
  - Identifier naming: meaningful names (NAMING-MEANINGFUL) and configurable regex conventions (NAMING-CONVENTION)
  - Function parameter count limit (HIS-PAR, configurable, default 5)
  - Optional clang-tidy execution when installed (prints a notice when absent, never skips silently)
  - Path-relative, machine-portable baseline fingerprints with fingerprint_policy_version verification
  - Fail-closed reporting of missing AST/compile tools (TOOL-MISSING) unless --allow-missing-tools is passed
  - Optional plugin hook interface for custom or platform-specific rules (no platform plugin shipped)

Exit codes:
  0 - All checks passed (or all findings matched baseline allowances / allowed missing tools).
  1 - Lint findings detected or baseline regression (new or excess findings).
  2 - Usage error, invalid configuration, baseline schema/policy mismatch, or execution error.
"""

from __future__ import annotations

import argparse
import bisect
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import fnmatch
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

_REPO_ROOT_STR = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from enforcement.checks.deviations import verify_deviation  # noqa: E402
from runner.safe_git import GitSecurityError, run_git  # noqa: E402

from runner.workspace_scope import WorkspaceScope, resolve_workspace_scope  # noqa: E402

VERSION = "0.1.0"

C_EXTS = {".c"}
CPP_EXTS = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h"}
HEADER_EXTS = {".hpp", ".hh", ".h"}
SOURCE_EXTS = {".cpp", ".cc", ".cxx", ".c"}
SKIP_DIRS = {
    ".git",
    "build",
    "build-cov",
    "build-rel",
    "build-doxygen",
    "_deps",
    "third_party",
    "generated",
}

FALLBACK_CLANG_TIDY_CONFIG = """---
Checks: >
  -*,
  bugprone-*,
  -bugprone-easily-swappable-parameters,
  performance-*,
  modernize-*,
  -modernize-use-trailing-return-type,
  -modernize-use-nodiscard,
  google-readability-casting,
  readability-braces-around-statements
WarningsAsErrors: '*'
...
"""

DEFAULT_NON_MEANINGFUL_NAMES = {
    "tmp",
    "temp",
    "val",
    "var",
    "foo",
    "bar",
    "baz",
    "obj",
    "dat",
    "buf",
    "ptr",
}
DEFAULT_ALLOWED_LOOP_INDICES = {"i", "j", "k"}
DEFAULT_MAX_PARAMS = 5
BASELINE_SCHEMA_VERSION = 1
FINGERPRINT_POLICY_VERSION = 2
CACHE_SCHEMA_VERSION = 1
DEFAULT_BASELINE_SCOPE = "default"
DEFAULT_BASELINE_PROFILE = "release"

INCLUDE_ENV_VARS = (
    "CXX",
    "CC",
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
)

CPP_TOKEN_PATTERN = re.compile(
    r"//[^\n]*"
    r"|/\*[\s\S]*?\*/"
    r"|\s+"
    r'|(?:u8|u|U|L)?R"([^()\s\\]{0,16})\([\s\S]*?\)\1"'
    r'|(?:u8|u|U|L)?"(?:\\[\s\S]|[^"\\])*"'
    r"|(?:u8|u|U|L)?'(?:\\[\s\S]|[^'\\])*'"
    r"|[A-Za-z_][A-Za-z0-9_]*"
    r"|0[xX][0-9a-fA-F']+(?:\.[0-9a-fA-F']*)?(?:[pP][+-]?[0-9']+)?[uUlLfF]*"
    r"|0[bB][01']+[uUlL]*"
    r"|(?:[0-9][0-9']*\.[0-9']*|\.[0-9][0-9']*)(?:[eE][+-]?[0-9']+)?[fFlLuU]*"
    r"|[0-9][0-9']*(?:[eE][+-]?[0-9']+)?[uUlLfF]*"
    r"|<=>|->\*|\.\.\.|<<=|>>="
    r"|::|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|\.\*|##"
    r"|[{}()\[\].,:;?!~+\-*/%&|^=<>#@\\]"
)

DIAG_LOCATION_PATTERN = re.compile(
    r"^(.+?):(\d+):(?:(\d+):)?\s*(fatal error|error|warning):\s*(.+)$"
)

C_STYLE_CAST_TYPES = (
    r"(?:const\s+|volatile\s+|unsigned\s+|signed\s+)*"
    r"(?:int|long|short|char|float|double|bool|size_t|ssize_t|ptrdiff_t"
    r"|u?int(?:8|16|32|64|max|ptr)_t|std::[A-Za-z_]\w*)\s*\**"
    r"|(?:const\s+|volatile\s+)*[A-Za-z_]\w*\s*\*+"
)

C_STYLE_CAST_LINE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_>])\((?:{C_STYLE_CAST_TYPES})\)\s*(?=[A-Za-z0-9_(*&\"'])"
)

IMPLICIT_LAMBDA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_\]\)\s>])\[\s*[&=]\s*(?:,[^\]]*)?\]\s*(?:\(|mutable\b|constexpr\b|noexcept\b|->|\{)"
    r"|(?:^|[=(\[{,;:\s])\s*\[\s*[&=]\s*(?:,[^\]]*)?\]\s*(?:\(|mutable\b|constexpr\b|noexcept\b|->|\{)"
)


@dataclass
class Finding:
    """Single lint diagnostic finding."""

    file: str
    line: int
    rule: str
    severity: str
    message: str


@dataclass
class FileReport:
    """Lint report for a single checked file."""

    path: str
    findings: list[Finding] = field(default_factory=list)
    execution_errors: list[str] = field(default_factory=list)


@dataclass
class FingerprintContext:
    """Contextual metadata associated with a Finding for stable fingerprinting."""

    enclosing_symbol: str = ""
    tokens: tuple[str, ...] = ()
    comment_text: str = ""
    diagnostic_path: str = ""
    extra_roots: tuple[str, ...] = ()


@dataclass
class NamingConfig:
    """Configurable identifier naming conventions and meaningful-name settings."""

    check_meaningful: bool = True
    disallowed_names: set[str] = field(
        default_factory=lambda: set(DEFAULT_NON_MEANINGFUL_NAMES)
    )
    allowed_loop_indices: set[str] = field(
        default_factory=lambda: set(DEFAULT_ALLOWED_LOOP_INDICES)
    )
    class_regex: str = r"^[A-Z][A-Za-z0-9]*$"
    function_regex: str = r"^[a-z][A-Za-z0-9]*$"
    variable_regex: str = r"^(?:k[A-Z][A-Za-z0-9]*|m_[a-z][A-Za-z0-9]*|[a-z][A-Za-z0-9]*)$"
    parameter_regex: str = r"^[a-z][A-Za-z0-9]*$"
    member_regex: str = r"^(?:m_)?[a-z][A-Za-z0-9]*$"
    private_member_regex: str = r"^m_[a-z][A-Za-z0-9]*$"
    constant_regex: str = r"^k[A-Z][A-Za-z0-9]*$"
    namespace_regex: str = r"^[a-z][a-z0-9_]*$"

    def validate(self) -> None:
        """Validate that all configured naming regex patterns compile cleanly."""
        for attr_name in (
            "class_regex",
            "function_regex",
            "variable_regex",
            "parameter_regex",
            "member_regex",
            "private_member_regex",
            "constant_regex",
            "namespace_regex",
        ):
            pattern = getattr(self, attr_name)
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"invalid naming regex for '{attr_name}': {exc}") from exc


@dataclass
class LintConfig:
    """Resolved preflight linter configuration from preflight.toml and CLI flags."""

    std: str = "c++17"
    c_std: str = "c11"
    max_params: int = DEFAULT_MAX_PARAMS
    allow_missing_tools: bool = False
    skip_clang_tidy: bool = False
    no_exceptions: bool = True
    no_rtti: bool = True
    no_impl_in_header: bool = True
    allow_new_in_init: bool = False
    allowed_heap_paths: tuple[str, ...] = ()
    skip_dirs: set[str] = field(default_factory=lambda: set(SKIP_DIRS))
    exclude_globs: tuple[str, ...] = ()
    disabled_rules: set[str] = field(default_factory=set)
    exempt_param_prefixes: tuple[str, ...] = ()
    require_file_doxygen: bool = True
    require_param_direction: bool = True
    naming: NamingConfig = field(default_factory=NamingConfig)
    plugin_paths: tuple[str, ...] = ()
    deviation_id: str = ""


@dataclass
class LintPlugin:
    """Loaded external plugin implementing custom file or AST checks."""

    name: str
    module: Any
    supported_extensions: set[str] = field(default_factory=set)

    def check_file(
        self,
        path: pathlib.Path,
        raw_text: str,
        stripped_text: str,
        config: LintConfig,
    ) -> list[Finding]:
        """Invoke the plugin's check_file hook if defined."""
        hook = getattr(self.module, "check_file", None)
        if not callable(hook):
            return []
        raw_findings = hook(path, raw_text, stripped_text, config)
        return _coerce_plugin_findings(raw_findings, str(path))

    def check_ast_node(
        self,
        node: dict[str, Any],
        path: pathlib.Path,
        line: int,
        raw_text: str,
        config: LintConfig,
    ) -> list[Finding]:
        """Invoke the plugin's check_ast_node hook if defined."""
        hook = getattr(self.module, "check_ast_node", None)
        if not callable(hook):
            return []
        raw_findings = hook(node, path, line, raw_text, config)
        return _coerce_plugin_findings(raw_findings, str(path))


def _coerce_plugin_findings(raw_items: Any, default_file: str) -> list[Finding]:
    """Convert plugin-returned Finding objects or dicts into Finding instances."""
    if not raw_items:
        return []
    coerced: list[Finding] = []
    for item in raw_items:
        if isinstance(item, Finding):
            coerced.append(item)
        elif hasattr(item, "rule") and hasattr(item, "message") and hasattr(item, "line"):
            coerced.append(
                Finding(
                    file=str(getattr(item, "file", default_file)),
                    line=int(getattr(item, "line", 1)),
                    rule=str(getattr(item, "rule", "PLUGIN-RULE")),
                    severity=str(getattr(item, "severity", "error")),
                    message=str(getattr(item, "message", "")),
                )
            )
        elif isinstance(item, dict):
            coerced.append(
                Finding(
                    file=str(item.get("file", default_file)),
                    line=int(item.get("line", 1)),
                    rule=str(item.get("rule", "PLUGIN-RULE")),
                    severity=str(item.get("severity", "error")),
                    message=str(item.get("message", "")),
                )
            )
    return coerced


def load_plugins(plugin_paths: Sequence[str], base_dir: pathlib.Path) -> list[LintPlugin]:
    """Load external Python lint plugins from file paths."""
    sys.modules.setdefault("preflight_lint", sys.modules[__name__])
    loaded: list[LintPlugin] = []
    for raw_plugin in plugin_paths:
        plugin_path = pathlib.Path(raw_plugin)
        if not plugin_path.is_absolute():
            plugin_path = (base_dir / plugin_path).resolve()
        if not plugin_path.is_file():
            raise ValueError(f"plugin file does not exist: {raw_plugin}")
        mod_name = f"preflight_plugin_{plugin_path.stem}_{len(loaded)}"
        spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"failed to load plugin spec from: {plugin_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        exts_raw = getattr(module, "SUPPORTED_EXTENSIONS", ())
        supported_exts = {str(ext) for ext in exts_raw if isinstance(ext, str)}
        loaded.append(
            LintPlugin(
                name=plugin_path.stem,
                module=module,
                supported_extensions=supported_exts,
            )
        )
    return loaded


VALID_CPP_STDS: frozenset[str] = frozenset({"c++11", "c++14", "c++17", "c++20", "c++23"})
VALID_C_STDS: frozenset[str] = frozenset({"c89", "c90", "c99", "c11", "c17", "c23"})


def _require_bool(val: Any, field_name: str) -> bool:
    """Validate that a TOML boolean setting is genuinely a bool."""
    if not isinstance(val, bool):
        raise ValueError(f"{field_name} must be a boolean, got {val!r}")
    return val


def load_config_file(
    config_path: pathlib.Path,
    *,
    allow_config_plugins: bool = False,
) -> LintConfig:
    """Load and validate a preflight.toml configuration file."""
    if not config_path.is_file():
        raise ValueError(f"configuration file does not exist: {config_path}")
    try:
        raw_toml = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"failed to parse config file {config_path}: {exc}") from exc

    if not isinstance(raw_toml, dict):
        raise ValueError("configuration root must be a TOML table")

    general = raw_toml.get("general", {})
    if not isinstance(general, dict):
        raise ValueError("[general] in preflight.toml must be a table")
    doxygen = raw_toml.get("doxygen", {})
    if not isinstance(doxygen, dict):
        raise ValueError("[doxygen] in preflight.toml must be a table")
    naming_tbl = raw_toml.get("naming", {})
    if not isinstance(naming_tbl, dict):
        raise ValueError("[naming] in preflight.toml must be a table")
    plugins_tbl = raw_toml.get("plugins", {})
    if not isinstance(plugins_tbl, dict):
        raise ValueError("[plugins] in preflight.toml must be a table")

    def _get_gen(key: str, default: Any) -> Any:
        """Read a general configuration setting from [general] or top-level."""
        if key in general:
            return general[key]
        return raw_toml.get(key, default)

    config = LintConfig()
    std_val = _get_gen("std", config.std)
    if not isinstance(std_val, str) or std_val.strip() not in VALID_CPP_STDS:
        raise ValueError(
            f"std must be one of {', '.join(sorted(VALID_CPP_STDS))}, got {std_val!r}"
        )
    config.std = std_val.strip()

    c_std_val = _get_gen("c_std", config.c_std)
    if not isinstance(c_std_val, str) or c_std_val.strip() not in VALID_C_STDS:
        raise ValueError(
            f"c_std must be one of {', '.join(sorted(VALID_C_STDS))}, got {c_std_val!r}"
        )
    config.c_std = c_std_val.strip()

    max_params_val = _get_gen("max_params", config.max_params)
    if isinstance(max_params_val, bool) or not isinstance(max_params_val, int) or max_params_val < 1:
        raise ValueError(f"max_params must be a positive integer, got {max_params_val!r}")
    config.max_params = max_params_val
    config.allow_missing_tools = _require_bool(
        _get_gen("allow_missing_tools", config.allow_missing_tools), "allow_missing_tools"
    )
    config.skip_clang_tidy = _require_bool(
        _get_gen("skip_clang_tidy", config.skip_clang_tidy), "skip_clang_tidy"
    )
    config.no_exceptions = _require_bool(
        _get_gen("no_exceptions", config.no_exceptions), "no_exceptions"
    )
    config.no_rtti = _require_bool(_get_gen("no_rtti", config.no_rtti), "no_rtti")
    config.no_impl_in_header = _require_bool(
        _get_gen("no_impl_in_header", config.no_impl_in_header), "no_impl_in_header"
    )
    config.allow_new_in_init = _require_bool(
        _get_gen("allow_new_in_init", config.allow_new_in_init), "allow_new_in_init"
    )

    allowed_heap = _get_gen("allowed_heap_paths", list(config.allowed_heap_paths))
    if not isinstance(allowed_heap, list) or any(not isinstance(x, str) for x in allowed_heap):
        raise ValueError("allowed_heap_paths must be a list of strings")
    config.allowed_heap_paths = tuple(allowed_heap)

    skip_dirs_val = _get_gen("skip_dirs", None)
    if skip_dirs_val is not None:
        if not isinstance(skip_dirs_val, list) or any(not isinstance(x, str) for x in skip_dirs_val):
            raise ValueError("skip_dirs must be a list of strings")
        config.skip_dirs = set(skip_dirs_val)

    exclude_globs_val = _get_gen("exclude_globs", list(config.exclude_globs))
    if not isinstance(exclude_globs_val, list) or any(not isinstance(x, str) for x in exclude_globs_val):
        raise ValueError("exclude_globs must be a list of strings")
    config.exclude_globs = tuple(exclude_globs_val)

    disabled_val = _get_gen("disabled_rules", [])
    if not isinstance(disabled_val, list) or any(not isinstance(x, str) for x in disabled_val):
        raise ValueError("disabled_rules must be a list of strings")
    config.disabled_rules = set(disabled_val)

    exempt_prefixes_val = _get_gen("exempt_param_prefixes", [])
    if not isinstance(exempt_prefixes_val, list) or any(not isinstance(x, str) for x in exempt_prefixes_val):
        raise ValueError("exempt_param_prefixes must be a list of strings")
    config.exempt_param_prefixes = tuple(exempt_prefixes_val)

    config.require_file_doxygen = _require_bool(
        doxygen.get("require_file_doxygen", _get_gen("require_file_doxygen", config.require_file_doxygen)),
        "doxygen.require_file_doxygen",
    )
    config.require_param_direction = _require_bool(
        doxygen.get("require_param_direction", _get_gen("require_param_direction", config.require_param_direction)),
        "doxygen.require_param_direction",
    )

    naming = NamingConfig()
    if "check_meaningful" in naming_tbl:
        naming.check_meaningful = _require_bool(naming_tbl["check_meaningful"], "naming.check_meaningful")
    if "disallowed_names" in naming_tbl:
        raw_disallowed = naming_tbl["disallowed_names"]
        if not isinstance(raw_disallowed, list) or any(not isinstance(x, str) for x in raw_disallowed):
            raise ValueError("naming.disallowed_names must be a list of strings")
        naming.disallowed_names = {x.lower() for x in raw_disallowed}
    if "allowed_loop_indices" in naming_tbl:
        raw_indices = naming_tbl["allowed_loop_indices"]
        if not isinstance(raw_indices, list) or any(not isinstance(x, str) for x in raw_indices):
            raise ValueError("naming.allowed_loop_indices must be a list of strings")
        naming.allowed_loop_indices = set(raw_indices)

    for regex_key in (
        "class_regex",
        "function_regex",
        "variable_regex",
        "parameter_regex",
        "member_regex",
        "private_member_regex",
        "constant_regex",
        "namespace_regex",
    ):
        if regex_key in naming_tbl:
            val = naming_tbl[regex_key]
            if not isinstance(val, str):
                raise ValueError(f"naming.{regex_key} must be a string")
            setattr(naming, regex_key, val)
    naming.validate()
    config.naming = naming

    plugin_list = plugins_tbl.get("paths", _get_gen("plugins", []))
    if not isinstance(plugin_list, list) or any(not isinstance(x, str) for x in plugin_list):
        raise ValueError("plugins.paths must be a list of strings")
    if plugin_list and not allow_config_plugins:
        raise ValueError(
            "plugins.paths in config file is disallowed unless --allow-config-plugins is passed"
        )
    config.plugin_paths = tuple(plugin_list)

    return config


def tokenize_cpp_snippet(snippet: str) -> tuple[str, ...]:
    """
    @brief Tokenize a C/C++ snippet preserving literals and operators while dropping comments and whitespace.
    @param[in] snippet Source code slice to tokenize.
    @return Tuple of normalized source tokens.
    """
    joined_snippet = re.sub(r"\\\r?\n", "", snippet)
    tokens: list[str] = []
    for match in CPP_TOKEN_PATTERN.finditer(joined_snippet):
        token_text = match.group(0)
        if token_text.startswith("//"):
            continue
        if token_text.startswith("/*"):
            continue
        if token_text.isspace():
            continue
        tokens.append(token_text)
    return tuple(tokens)


def normalize_diagnostic_path(
    raw_path: str,
    repository_root: pathlib.Path,
    extra_roots: Sequence[tuple[pathlib.Path, str]] = (),
) -> str:
    """
    @brief Normalize a diagnostic or source path relative to repository_root or known build roots.
    @param[in] raw_path Raw file path string.
    @param[in] repository_root Repository root directory.
    @param[in] extra_roots Known build roots mapped to placeholder tokens.
    @return Stable normalized repository-relative POSIX path string.
    """
    if not raw_path:
        return ""
    cleaned_raw = raw_path.replace("\\", "/")
    candidate = pathlib.Path(cleaned_raw)
    if not candidate.is_absolute():
        norm_rel = pathlib.PurePosixPath(os.path.normpath(cleaned_raw)).as_posix()
        return "" if norm_rel == "." else norm_rel

    try:
        repo_resolved = repository_root.resolve()
        resolved_candidate = candidate.resolve()
        return resolved_candidate.relative_to(repo_resolved).as_posix()
    except (OSError, ValueError):
        pass
    try:
        return candidate.relative_to(repository_root).as_posix()
    except ValueError:
        pass
    for root_path, token in extra_roots:
        try:
            root_resolved = root_path.resolve()
            rel_extra = candidate.resolve().relative_to(root_resolved).as_posix()
            return token if rel_extra == "." else f"{token}/{rel_extra}"
        except (OSError, ValueError):
            pass
        try:
            rel_extra = candidate.relative_to(root_path).as_posix()
            return token if rel_extra == "." else f"{token}/{rel_extra}"
        except ValueError:
            continue
    return cleaned_raw


def _replace_known_roots_in_text(
    text: str,
    repository_root: pathlib.Path,
    extra_roots: Sequence[tuple[pathlib.Path, str]],
) -> str:
    """
    @brief Replace absolute checkout and build root prefixes inside diagnostic text.
    @param[in] text Diagnostic message text.
    @param[in] repository_root Repository root directory.
    @param[in] extra_roots Known build roots mapped to placeholder tokens.
    @return Text with known absolute roots normalized.
    """
    normalized = text
    replacements: list[tuple[str, str]] = []
    for root_path, token in extra_roots:
        replacements.append((str(root_path), token))
        try:
            replacements.append((str(root_path.resolve()), token))
        except OSError:
            pass
    try:
        resolved_repo_str = str(repository_root.resolve())
        replacements.append((resolved_repo_str + "/", ""))
        replacements.append((resolved_repo_str, "<repo>"))
    except OSError:
        pass
    repo_str = str(repository_root)
    replacements.append((repo_str + "/", ""))
    replacements.append((repo_str, "<repo>"))
    unique_pairs = sorted(set(replacements), key=lambda item: len(item[0]), reverse=True)
    for prefix, token in unique_pairs:
        if prefix:
            normalized = normalized.replace(prefix, token)
    return normalized


def normalize_diagnostic_message(
    raw_message: str,
    repository_root: pathlib.Path,
    extra_roots: Sequence[tuple[pathlib.Path, str]] = (),
) -> str:
    """
    @brief Normalize a diagnostic message without stripping meaningful numbers.
    @param[in] raw_message Raw diagnostic message text.
    @param[in] repository_root Repository root directory.
    @param[in] extra_roots Known build roots mapped to placeholder tokens.
    @return Normalized diagnostic message string.
    """
    stripped_msg = raw_message.strip()
    match = DIAG_LOCATION_PATTERN.match(stripped_msg)
    if match is not None:
        severity_label = match.group(4)
        body_text = match.group(5).strip()
        combined = f"{severity_label}: {body_text}"
        return _replace_known_roots_in_text(combined, repository_root, extra_roots)
    return _replace_known_roots_in_text(stripped_msg, repository_root, extra_roots)


def finding_fingerprint(
    finding: Finding,
    repository_root: pathlib.Path | str,
    context: FingerprintContext | dict[str, Any] | None = None,
    extra_roots: Sequence[tuple[pathlib.Path, str]] = (),
) -> str:
    """
    @brief Compute a stable SHA-256 fingerprint for a Finding from its semantic context.
    @param[in] finding Finding instance to fingerprint.
    @param[in] repository_root Repository root used for path normalization.
    @param[in] context Optional FingerprintContext or dictionary with tokens/symbol/comment.
    @param[in] extra_roots Optional build-root replacement pairs.
    @return Lowercase 64-character SHA-256 hex digest.
    """
    repo_path = pathlib.Path(repository_root)
    enclosing_symbol = ""
    tokens: list[str] = []
    comment_text = ""
    diagnostic_path = ""
    ctx_extra_roots: list[tuple[pathlib.Path, str]] = list(extra_roots)
    if isinstance(context, FingerprintContext):
        enclosing_symbol = context.enclosing_symbol
        tokens = list(context.tokens)
        comment_text = context.comment_text
        diagnostic_path = context.diagnostic_path
        if not ctx_extra_roots and context.extra_roots:
            ctx_extra_roots = [(pathlib.Path(item), "<BUILD_ROOT>") for item in context.extra_roots]
    elif isinstance(context, dict):
        enclosing_symbol = str(context.get("enclosing_symbol", ""))
        tokens = [str(item) for item in context.get("tokens", ())]
        comment_text = str(context.get("comment_text", context.get("comment", "")))
        diagnostic_path = str(context.get("diagnostic_path", ""))

    if not diagnostic_path:
        diag_match = DIAG_LOCATION_PATTERN.match(finding.message.strip())
        if diag_match is not None:
            diagnostic_path = diag_match.group(1)
        else:
            diagnostic_path = finding.file
    diagnostic_path = normalize_diagnostic_path(
        diagnostic_path, repo_path, ctx_extra_roots
    )

    normalized_message = normalize_diagnostic_message(
        finding.message, repo_path, ctx_extra_roots
    )
    payload = {
        "comment": comment_text,
        "enclosing_symbol": enclosing_symbol,
        "message": normalized_message,
        "path": diagnostic_path,
        "rule": finding.rule,
        "severity": finding.severity,
        "tokens": tokens,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def relocate_owned_path(raw_path: str, old_root: pathlib.Path, new_root: pathlib.Path) -> str:
    """Relocate only paths owned by old_root, respecting filesystem path boundaries."""
    try:
        relative = pathlib.Path(raw_path).resolve().relative_to(old_root.resolve())
    except (OSError, ValueError):
        return raw_path
    return str(new_root / relative)


def normalized_finding_export(
    findings: Sequence[Finding],
    repository_root: pathlib.Path,
    contexts: dict[int, FingerprintContext],
) -> list[dict[str, Any]]:
    """Export normalized diagnostic payloads as a deterministic occurrence multiset."""
    grouped: dict[tuple[str, str, str, str, str], int] = {}
    for finding in findings:
        context = contexts.get(id(finding))
        roots = [(pathlib.Path(root), "<BUILD_ROOT>") for root in context.extra_roots] if context else []
        path = normalize_diagnostic_path(
            context.diagnostic_path if context and context.diagnostic_path else finding.file,
            repository_root,
            roots,
        )
        message = normalize_diagnostic_message(finding.message, repository_root, roots)
        fingerprint = finding_fingerprint(finding, repository_root, context)
        key = (fingerprint, path, message, finding.rule, finding.severity)
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {
            "fingerprint": k[0],
            "path": k[1],
            "message": k[2],
            "rule": k[3],
            "severity": k[4],
            "occurrence_count": v,
        }
        for k, v in sorted(grouped.items())
    ]


def filter_findings_by_changed_paths(
    findings: Sequence[Finding],
    contexts: dict[int, FingerprintContext],
    repository_root: pathlib.Path,
    changed_paths: set[str],
) -> list[Finding]:
    """Keep findings whose effective diagnostic location is in the changed-file set."""
    kept: list[Finding] = []
    for finding in findings:
        context = contexts.get(id(finding))
        diagnostic_path = context.diagnostic_path if context and context.diagnostic_path else finding.file
        if normalize_diagnostic_path(diagnostic_path, repository_root) in changed_paths:
            kept.append(finding)
    return kept


def strip_comments_and_strings(text: str) -> str:
    """Replace C/C++ comments and string/char literals with spaces while preserving newlines."""
    pattern = re.compile(
        r'//[^\n]*|/\*[\s\S]*?\*/|R"([^(]*)\([\s\S]*?\)\1"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        re.MULTILINE,
    )

    def _blank(match: re.Match[str]) -> str:
        """Replace matched comment or literal with line-preserving spaces/newlines."""
        raw = match.group(0)
        if raw.startswith(('"', "'", "R")):
            return '""' + ("\n" * raw.count("\n"))
        return "\n" * raw.count("\n")

    return pattern.sub(_blank, text)


def collect_files(
    targets: Sequence[str],
    extra_extensions: set[str] | None = None,
    skip_dirs: set[str] | None = None,
) -> list[pathlib.Path]:
    """Expand files and directories into supported C/C++ (and plugin) files."""
    valid_exts = C_EXTS | CPP_EXTS | (extra_extensions or set())
    active_skip_dirs = skip_dirs if skip_dirs is not None else SKIP_DIRS
    collected: list[pathlib.Path] = []
    for raw in targets:
        p = pathlib.Path(raw).resolve()
        if not p.exists():
            print(f"error: path does not exist: {raw}", file=sys.stderr)
            sys.exit(2)
        if p.is_file():
            if p.suffix in valid_exts:
                collected.append(p)
        else:
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(
                    d for d in dirs if d not in active_skip_dirs and not d.startswith("build")
                )
                for name in sorted(files):
                    fp = pathlib.Path(root) / name
                    if fp.suffix in valid_exts:
                        collected.append(fp)
    return collected


def detect_gcc_install_dir() -> str | None:
    """Detect GCC installation directory from CXX/g++ so clang/clang-tidy find libstdc++."""
    compiler = os.environ.get("CXX", "g++")
    if not shutil.which(compiler):
        return None
    res = subprocess.run(
        [compiler, "-print-libgcc-file-name"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    gcc_dir = pathlib.Path(res.stdout.strip()).resolve().parent
    return str(gcc_dir) if (gcc_dir / "libgcc.a").is_file() else None


def find_repo_clang_tidy(
    start: pathlib.Path,
    repository_root: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """Walk up from target directory to repository_root to find .clang-tidy."""
    stop_dir = repository_root.resolve() if repository_root is not None else None
    base = start.resolve().parent if start.is_file() else start.resolve()
    for cur in [base, *base.parents]:
        candidate = cur / ".clang-tidy"
        if candidate.is_file():
            return candidate
        if stop_dir is not None and cur == stop_dir:
            break
    return None


def _extract_flags_from_entries(
    entries: Sequence[dict[str, Any]],
    fallback_dir: pathlib.Path,
    target: pathlib.Path,
) -> list[str]:
    """
    @brief Extract -I, -isystem, -D, -std flags from parsed compile_commands entries.
    @param[in] entries Parsed compile_commands.json entries.
    @param[in] fallback_dir Default directory when an entry omits 'directory'.
    @param[in] target Target source or header file path.
    @return List of extracted compiler flags.
    """
    target_resolved = str(target.resolve())
    matched = None
    for entry in entries:
        raw_file = entry.get("file", "")
        if not isinstance(raw_file, str) or not raw_file:
            continue
        work_dir = pathlib.Path(entry.get("directory", fallback_dir))
        if not work_dir.is_absolute():
            work_dir = fallback_dir / work_dir
        file_path = pathlib.Path(raw_file)
        if not file_path.is_absolute():
            file_path = work_dir / file_path
        if str(file_path.resolve()) == target_resolved:
            matched = entry
            break
    if matched is None and entries:
        matched = entries[0]
    if matched is None:
        return []

    args = matched.get("arguments") or shlex.split(matched.get("command", ""))
    directory = pathlib.Path(matched.get("directory", fallback_dir))
    if not directory.is_absolute():
        directory = fallback_dir / directory
    extracted: list[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in ("-I", "-isystem", "-D", "-iquote") and idx + 1 < len(args):
            val = args[idx + 1]
            if arg in ("-I", "-isystem", "-iquote") and not pathlib.Path(val).is_absolute():
                val = str((directory / val).resolve())
            extracted.extend([arg, val])
            idx += 2
            continue
        if arg.startswith(("-I", "-isystem", "-D", "-std=")):
            if arg.startswith("-I") and len(arg) > 2 and not pathlib.Path(arg[2:]).is_absolute():
                extracted.append("-I" + str((directory / arg[2:]).resolve()))
            else:
                extracted.append(arg)
        idx += 1
    return extracted


def _has_compile_database_entry(
    entries: Sequence[dict[str, Any]],
    fallback_dir: pathlib.Path,
    target: pathlib.Path,
) -> bool:
    """Return whether the compile database contains an exact entry for a source file."""
    target_resolved = target.resolve()
    for entry in entries:
        source_file = entry.get("file")
        if not isinstance(source_file, str) or not source_file:
            continue
        working_dir = pathlib.Path(entry.get("directory", fallback_dir))
        if not working_dir.is_absolute():
            working_dir = fallback_dir / working_dir
        source_path = pathlib.Path(source_file)
        if not source_path.is_absolute():
            source_path = working_dir / source_path
        if source_path.resolve() == target_resolved:
            return True
    return False


def extract_compile_flags(compile_commands: pathlib.Path | None, target: pathlib.Path) -> list[str]:
    """Extract -I, -isystem, -D, -std flags from compile_commands.json for target."""
    if compile_commands is None or not compile_commands.is_file():
        return []
    try:
        entries = json.loads(compile_commands.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return _extract_flags_from_entries(entries, compile_commands.parent, target)


def infer_repo_include_flags(target: pathlib.Path) -> list[str]:
    """Infer common repository include directories (e.g., include, src) when no compile_commands."""
    flags: list[str] = []
    for cur in target.parents:
        if (cur / "CMakeLists.txt").is_file() or (cur / ".git").exists() or (cur / "preflight.toml").is_file():
            for subdir in ("include", "src"):
                if (cur / subdir).is_dir():
                    flags.extend(["-I", str(cur / subdir)])
            flags.extend(["-I", str(cur)])
            break
    return flags


def is_heap_allowed_for_path(
    path: pathlib.Path,
    repository_root: pathlib.Path | None,
    allow_new_in_init: bool,
    allowed_heap_paths: Sequence[str] = (),
) -> bool:
    """Return True if dynamic heap allocation (new/delete/malloc/free) is allowed for path."""
    if allow_new_in_init and "init" in path.name.lower():
        return True
    if not allowed_heap_paths:
        return False
    rel_posix = (
        normalize_diagnostic_path(str(path), repository_root)
        if repository_root is not None
        else path.name
    )
    filename = path.name
    full_posix = path.as_posix()
    for pattern in allowed_heap_paths:
        if (
            fnmatch.fnmatch(rel_posix, pattern)
            or fnmatch.fnmatch(filename, pattern)
            or fnmatch.fnmatch(full_posix, pattern)
        ):
            return True
    return False


def _record_comment_context(
    contexts: dict[int, FingerprintContext] | None,
    finding: Finding,
    comment_line: str,
) -> None:
    """
    @brief Attach normalized comment text to a comment-rule finding context.
    @param[in,out] contexts Optional mapping from Finding id to FingerprintContext.
    @param[in] finding Finding instance to annotate.
    @param[in] comment_line Raw offending comment line.
    @return None.
    """
    if contexts is None:
        return
    normalized_comment = " ".join(comment_line.split())
    contexts[id(finding)] = FingerprintContext(comment_text=normalized_comment)


def _direction_type_shape(resolved: str) -> str:
    """Remove balanced template arguments and function declarators from an AST type.

    Clang's JSON parameter nodes expose resolved type spellings rather than
    pointee type nodes. Nested pointers and const qualifiers belong to those
    nested types, not the outer parameter. Function pointers passed by value
    cannot write output through their function pointee.
    """
    shape: list[str] = []
    closing: list[str] = []
    for character in resolved:
        if character in "<(":
            closing.append(">" if character == "<" else ")")
        elif closing and character == closing[-1]:
            closing.pop()
        elif not closing:
            shape.append(character)
    return "".join(shape)


def check_param_direction_types(
    node: dict[str, Any], raw_text: str, target_file: str,
    findings: list[Finding],
) -> None:
    """Compare attached Doxygen directions with resolved AST parameter types."""
    start = node.get("range", {}).get("begin", {}).get("offset")
    if not isinstance(start, int):
        return
    prefix = raw_text.encode("utf-8")[:start].decode("utf-8")
    comments = [m for m in CPP_TOKEN_PATTERN.finditer(prefix)
                if m.group().startswith(("/**", "/*!", "///", "//!"))]
    if not comments:
        return
    last = comments[-1]
    if prefix[last.end():].strip():
        return
    first = len(comments) - 1
    if last.group().startswith(("///", "//!")):
        while first > 0 and not prefix[comments[first - 1].end():comments[first].start()].strip():
            first -= 1
    documentation = prefix[comments[first].start():last.end()]
    directions = dict((m.group(2), m.group(1).replace(" ", "")) for m in
                      re.finditer(r"[@\\]param\s*\[\s*(in\s*,\s*out|in|out)\s*\]\s+(\w+)", documentation))
    for param in node.get("inner", []):
        if param.get("kind") != "ParmVarDecl":
            continue
        name = param.get("name", "")
        direction = directions.get(name)
        if direction is None:
            continue
        types = param.get("type", {})
        spelling = types.get("qualType", "")
        resolved = types.get("desugaredQualType", spelling)
        # Top-level pointer constness does not make its pointee immutable.
        shape = _direction_type_shape(resolved)
        indirect = re.search(r"[*&]", shape)
        readonly = indirect is None
        if indirect:
            pointee = re.split(r"[*&]", shape)[-2]
            readonly = bool(re.search(r"\bconst\b", pointee))
        severity = "error" if readonly and direction != "in" else (
            "warning" if not readonly and direction == "in" else None)
        if severity:
            findings.append(Finding(target_file, prefix.count("\n") + 1,
                                    "DOXYGEN-PARAM-DIR-MISMATCH", severity,
                                    f"Parameter '{name}' of type '{spelling}' documented as [{direction}] "
                                    + ("cannot provide output" if readonly else "may provide output")))


def check_comments_and_doxygen(
    raw_text: str,
    target_file: str,
    findings: list[Finding],
    contexts: dict[int, FingerprintContext] | None = None,
    require_param_direction: bool = True,
    disabled_rules: set[str] | None = None,
) -> None:
    """Check commented-out code / empty comments and Doxygen @param[in|out|in,out] directions."""
    disabled = disabled_rules or set()
    if "DOXYGEN-FILE-BLOCK" not in disabled:
        for token in CPP_TOKEN_PATTERN.finditer(raw_text):
            comment = token.group()
            if not comment.startswith(("//", "/*")) or comment.startswith(("/**", "/*!", "///", "//!")):
                continue
            for tag in re.finditer(r"[@\\]file\b", comment):
                finding = Finding(target_file, raw_text.count("\n", 0, token.start() + tag.start()) + 1,
                                  "DOXYGEN-FILE-BLOCK", "error",
                                  "@file must be inside a Doxygen comment")
                findings.append(finding)
                _record_comment_context(contexts, finding, comment)
    lines = raw_text.splitlines()
    for idx, line in enumerate(lines, start=1):
        stripped_line = line.strip()

        if require_param_direction and "DOXYGEN-PARAM-DIR" not in disabled:
            for _ in re.finditer(r"[@\\]param\b(?!\s*\[(in|out|in,out)\])", line):
                finding = Finding(
                    target_file,
                    idx,
                    "DOXYGEN-PARAM-DIR",
                    "error",
                    "@param must specify direction: @param[in], @param[out], or @param[in,out]",
                )
                findings.append(finding)
                _record_comment_context(contexts, finding, stripped_line)

        if stripped_line.startswith("//") and not stripped_line.startswith(("///", "//!")):
            body = stripped_line[2:].strip()
            if not body:
                if "MISRA-COMMENT-REDUNDANT" not in disabled:
                    finding = Finding(
                        target_file,
                        idx,
                        "MISRA-COMMENT-REDUNDANT",
                        "error",
                        "Empty comment; remove redundant comments or explain WHY the code exists",
                    )
                    findings.append(finding)
                    _record_comment_context(contexts, finding, stripped_line)
            elif (
                "MISRA-5.7.2" not in disabled
                and not any(
                    tag in body
                    for tag in (
                        "NOLINT",
                        "BEGIN GENERATED",
                        "END GENERATED",
                        "@file",
                        "@brief",
                        "@note",
                        "----",
                    )
                )
                and re.search(
                    r"(^\s*#include\b|^\s*(return|if|for|while|switch)\s*[\(\s]|;\s*$|\b[a-zA-Z_]\w*\s*\([^)]*\)\s*;)",
                    body,
                )
            ):
                if re.search(
                    r"(^\s*#include\b|^\s*return\b[^;]*;|^\s*(if|for|while|switch)\s*\(|\w+\([^)]*\)\s*;|^\s*\w+\s*=\s*[^=]+;)",
                    body,
                ):
                    finding = Finding(
                        target_file,
                        idx,
                        "MISRA-5.7.2",
                        "error",
                        "Commented-out code forbidden; remove dead code or explain WHY if keeping a note",
                    )
                    findings.append(finding)
                    _record_comment_context(contexts, finding, stripped_line)


def _switch_has_default(node: dict[str, Any]) -> bool:
    """Return True if a SwitchStmt AST subtree contains a DefaultStmt."""
    if node.get("kind") == "DefaultStmt":
        return True
    for child in node.get("inner", []):
        if isinstance(child, dict) and child.get("kind") != "SwitchStmt":
            if _switch_has_default(child):
                return True
    return False


def _has_compound_body(node: dict[str, Any]) -> bool:
    """Return True if an AST function/method node has a CompoundStmt body."""
    return any(isinstance(c, dict) and c.get("kind") == "CompoundStmt" for c in node.get("inner", []))


def _is_vla_qual_type(qual_type: str) -> bool:
    """Return True if a VarDecl qualType represents a Variable Length Array."""
    if "VariableArrayType" in qual_type:
        return True
    return bool(re.search(r"\[(?![0-9]+\])[A-Za-z_][^\]]*\]", qual_type))


def _build_line_start_offsets(raw_text: str) -> list[int]:
    """
    @brief Compute 0-based UTF-8 byte offsets for the start of every 1-based line in raw_text.
    @param[in] raw_text Full source file text.
    @return Sorted list of byte offsets where index i corresponds to line i + 1.
    """
    raw_bytes = raw_text.encode("utf-8")
    line_starts = [0]
    search_pos = 0
    while True:
        newline_pos = raw_bytes.find(b"\n", search_pos)
        if newline_pos == -1:
            break
        line_starts.append(newline_pos + 1)
        search_pos = newline_pos + 1
    return line_starts


def _offset_to_line(line_starts: list[int], byte_offset: int) -> int:
    """
    @brief Convert a 0-based UTF-8 byte offset to a 1-based physical line number.
    @param[in] line_starts Precomputed line-start byte offsets from _build_line_start_offsets.
    @param[in] byte_offset 0-based byte offset from Clang AST JSON.
    @return 1-based line number, or 0 if byte_offset is negative.
    """
    if byte_offset < 0 or not line_starts:
        return 0
    return bisect.bisect_right(line_starts, byte_offset)


class _AstLocationCursor:
    """Stateful pre-order location cursor matching Clang JSON AST location compression."""

    def __init__(self, target_file: str | pathlib.Path, raw_text: str) -> None:
        """
        @brief Initialize cursor state for a target source or header file.
        @param[in] target_file Path to the file being linted.
        @param[in] raw_text Full source text of target_file for offset-to-line recovery.
        """
        self.target_resolved = pathlib.Path(target_file).resolve()
        self.line_starts = _build_line_start_offsets(raw_text)
        self.cur_file: str | None = None
        self.cur_line: int = 0
        self._is_target_cache: dict[str, bool] = {}

    def is_target_file(self, file_path: str | None) -> bool:
        """
        @brief Check whether file_path resolves to the target file.
        @param[in] file_path File path string from AST location or None.
        @return True if file_path resolves to target_resolved.
        """
        if not file_path:
            return False
        cached = self._is_target_cache.get(file_path)
        if cached is not None:
            return cached
        try:
            is_match = pathlib.Path(file_path).resolve() == self.target_resolved
        except OSError:
            is_match = False
        self._is_target_cache[file_path] = is_match
        return is_match

    def _advance_single_loc(self, loc_dict: dict[str, Any]) -> tuple[str | None, int]:
        """
        @brief Update cursor state from a single non-macro location dictionary.
        @param[in] loc_dict Clang AST location object.
        @return Updated (cur_file, cur_line) after applying loc_dict.
        """
        explicit_file = loc_dict.get("file")
        if isinstance(explicit_file, str) and explicit_file:
            self.cur_file = explicit_file

        explicit_line = loc_dict.get("line")
        line_val = int(explicit_line, 0) if isinstance(explicit_line, str) else int(explicit_line or 0)
        if line_val > 0:
            self.cur_line = line_val
        else:
            byte_offset = loc_dict.get("offset")
            if isinstance(byte_offset, int) and self.is_target_file(self.cur_file):
                recovered_line = _offset_to_line(self.line_starts, byte_offset)
                if recovered_line > 0:
                    self.cur_line = recovered_line
        return self.cur_file, self.cur_line

    def _advance_loc_entry(self, entry: Any) -> tuple[str | None, int, bool]:
        """
        @brief Update cursor state from a loc or range endpoint entry in Clang emission order.
        @param[in] entry Location dictionary (plain or macro spellingLoc/expansionLoc).
        @return Tuple of (effective_file, effective_line, has_location_data).
        """
        if not isinstance(entry, dict) or not entry:
            return self.cur_file, self.cur_line, False

        spelling = entry.get("spellingLoc")
        expansion = entry.get("expansionLoc")
        has_macro_locs = (
            (isinstance(spelling, dict) and bool(spelling))
            or (isinstance(expansion, dict) and bool(expansion))
        )
        if has_macro_locs:
            if isinstance(spelling, dict) and spelling:
                self._advance_single_loc(spelling)
            if isinstance(expansion, dict) and expansion:
                self._advance_single_loc(expansion)
            return self.cur_file, self.cur_line, True

        eff_file, eff_line = self._advance_single_loc(entry)
        return eff_file, eff_line, True

    def advance_node(self, node: dict[str, Any], parent_in_target: bool) -> tuple[str | None, int, bool]:
        """
        @brief Advance the pre-order cursor across a node's loc, range.begin, and range.end.
        @param[in] node Clang AST JSON node dictionary.
        @param[in] parent_in_target Ownership flag inherited only when loc and range are absent.
        @return Tuple of (node_file, node_line, in_target) for this node.
        """
        loc_file, loc_line, has_loc = self._advance_loc_entry(node.get("loc"))
        rng = node.get("range")
        begin_entry = rng.get("begin") if isinstance(rng, dict) else None
        end_entry = rng.get("end") if isinstance(rng, dict) else None
        begin_file, begin_line, has_begin = self._advance_loc_entry(begin_entry)
        _, _, has_end = self._advance_loc_entry(end_entry)

        if has_loc:
            node_file, node_line = loc_file, loc_line
            in_target = self.is_target_file(node_file)
        elif has_begin:
            node_file, node_line = begin_file, begin_line
            in_target = self.is_target_file(node_file)
        elif has_end:
            node_file, node_line = self.cur_file, self.cur_line
            in_target = self.is_target_file(node_file)
        else:
            node_file, node_line = self.cur_file, self.cur_line
            in_target = parent_in_target

        return node_file, node_line, in_target

    def peek_node_line(self, node: dict[str, Any], fallback_line: int) -> int:
        """
        @brief Inspect a node's line without mutating the traversal cursor.
        @param[in] node Clang AST JSON node dictionary.
        @param[in] fallback_line Line number to return when no line or offset is available.
        @return Resolved 1-based line number for node, or fallback_line.
        """
        rng = node.get("range")
        candidates = (
            node.get("loc"),
            rng.get("begin") if isinstance(rng, dict) else None,
        )
        for entry in candidates:
            if not isinstance(entry, dict) or not entry:
                continue
            eff = entry.get("expansionLoc", entry)
            if not isinstance(eff, dict):
                continue
            explicit_line = eff.get("line")
            line_val = int(explicit_line or 0) if explicit_line is not None else 0
            if line_val > 0:
                return line_val
            byte_offset = eff.get("offset")
            if isinstance(byte_offset, int):
                eff_file = eff.get("file")
                if eff_file is None or self.is_target_file(eff_file):
                    recovered_line = _offset_to_line(self.line_starts, byte_offset)
                    if recovered_line > 0:
                        return recovered_line
        return fallback_line


def _extract_ast_node_slice(node: dict[str, Any], raw_text: str) -> str:
    """
    @brief Extract source text for an AST node using its begin/end byte offsets.
    @param[in] node Clang AST JSON node dictionary.
    @param[in] raw_text Full source file text.
    @return Source substring covered by the node range, or empty string if unavailable.
    """
    rng = node.get("range")
    if not isinstance(rng, dict):
        return ""
    begin = rng.get("begin")
    end = rng.get("end")
    if not isinstance(begin, dict) or not isinstance(end, dict):
        return ""
    eff_begin = begin.get("expansionLoc", begin)
    eff_end = end.get("expansionLoc", end)
    if not isinstance(eff_begin, dict) or not isinstance(eff_end, dict):
        return ""
    begin_offset = eff_begin.get("offset")
    end_offset = eff_end.get("offset")
    end_tok_len = eff_end.get("tokLen", 0)
    if not isinstance(begin_offset, int) or not isinstance(end_offset, int):
        return ""
    if not isinstance(end_tok_len, int):
        end_tok_len = 0
    raw_bytes = raw_text.encode("utf-8")
    end_pos = min(len(raw_bytes), end_offset + max(end_tok_len, 1))
    if begin_offset < 0 or begin_offset >= len(raw_bytes) or end_pos <= begin_offset:
        return ""
    return raw_bytes[begin_offset:end_pos].decode("utf-8", errors="replace")


def _clean_base_class_name(qual_type: str) -> str:
    """Normalize a base class qualType string to its unqualified class identifier."""
    without_keywords = re.sub(r"\b(class|struct|const|volatile)\b", "", qual_type).strip()
    without_template = without_keywords.split("<", 1)[0].strip()
    return without_template.split("::")[-1].strip()


def _collect_ast_class_hierarchy(
    root: Any,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """
    @brief Collect base classes and virtual method names for all CXXRecordDecl nodes in an AST.
    @param[in] root Clang AST JSON root node.
    @return Tuple of (class_bases_map, class_virtual_methods_map).
    """
    class_bases: dict[str, set[str]] = {}
    class_virtuals: dict[str, set[str]] = {}
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "CXXRecordDecl" and not node.get("isImplicit"):
            cls_name = str(node.get("name", ""))
            if cls_name:
                bases = class_bases.setdefault(cls_name, set())
                for base_entry in node.get("bases", []):
                    if isinstance(base_entry, dict):
                        b_type = str(base_entry.get("type", {}).get("qualType", ""))
                        b_clean = _clean_base_class_name(b_type)
                        if b_clean:
                            bases.add(b_clean)
                v_methods = class_virtuals.setdefault(cls_name, set())
                for child in node.get("inner", []):
                    if not isinstance(child, dict) or child.get("isImplicit"):
                        continue
                    c_kind = child.get("kind")
                    if c_kind == "CXXMethodDecl":
                        m_name = str(child.get("name", ""))
                        has_override_attr = any(
                            isinstance(sub, dict) and sub.get("kind") in ("OverrideAttr", "FinalAttr")
                            for sub in child.get("inner", [])
                        )
                        if m_name and (child.get("virtual") or has_override_attr):
                            v_methods.add(m_name)
        for child in node.get("inner", []):
            if isinstance(child, dict):
                stack.append(child)
    return class_bases, class_virtuals


def _get_ancestor_virtual_methods(
    cls_name: str,
    class_bases: dict[str, set[str]],
    class_virtuals: dict[str, set[str]],
) -> set[str]:
    """Return all virtual method names inherited from direct or transitive base classes."""
    inherited: set[str] = set()
    visited: set[str] = set()
    queue = list(class_bases.get(cls_name, ()))
    while queue:
        base_name = queue.pop()
        if base_name in visited:
            continue
        visited.add(base_name)
        inherited.update(class_virtuals.get(base_name, ()))
        queue.extend(class_bases.get(base_name, ()))
    return inherited


def _is_ctor_violation_misra_15_1_3(
    node: dict[str, Any],
    raw_text: str,
    cur_line: int,
) -> bool:
    """Return True for an in-class constructor callable with one argument and lacking explicit."""
    if node.get("kind") != "CXXConstructorDecl" or node.get("isImplicit"):
        return False
    if node.get("previousDecl"):
        return False
    if node.get("explicitlyDeleted") or node.get("explicitlyDefaulted"):
        return False
    ctor_name = str(node.get("name", ""))
    if not ctor_name:
        return False
    params = [
        c for c in node.get("inner", [])
        if isinstance(c, dict) and c.get("kind") == "ParmVarDecl"
    ]
    if not params:
        return False
    required_params = [
        p for p in params
        if not p.get("init") and not p.get("inner")
    ]
    if len(required_params) > 1:
        return False

    p0_type = str(params[0].get("type", {}).get("qualType", ""))
    clean_p0 = _clean_base_class_name(
        re.sub(r"\b(?:const|volatile)\b|[&\s]", "", p0_type)
    )
    if clean_p0 == ctor_name and "&" in p0_type and len(required_params) == 1:
        return False

    if node.get("explicit") is True:
        return False

    node_slice = _extract_ast_node_slice(node, raw_text)
    if node_slice:
        sig_prefix = node_slice.split("(", 1)[0]
        if re.search(r"\bexplicit\b", sig_prefix):
            return False

    lines = raw_text.splitlines()
    if 1 <= cur_line <= len(lines):
        start_idx = max(0, cur_line - 2)
        line_window = " ".join(lines[start_idx:cur_line])
        if re.search(rf"\bexplicit\b[^;{{}}]*\b{re.escape(ctor_name)}\s*\(", line_window):
            return False
        if re.search(rf"\b{re.escape(ctor_name)}\s*\([^)]*\)\s*=\s*(default|delete)\b", lines[cur_line - 1]):
            return False

    return True


def _is_method_violation_misra_13_3_1(
    node: dict[str, Any],
    enclosing_class: str,
    has_bases: bool,
    ancestor_virtuals: set[str],
    raw_text: str,
    cur_line: int,
) -> bool:
    """Return True if a CXXMethodDecl overrides a base virtual method without override or final."""
    if not has_bases or node.get("kind") != "CXXMethodDecl" or node.get("isImplicit"):
        return False
    if node.get("storageClass") == "static":
        return False
    method_name = str(node.get("name", ""))
    if not method_name or method_name.startswith("operator"):
        return False

    has_override_attr = any(
        isinstance(sub, dict) and sub.get("kind") in ("OverrideAttr", "FinalAttr")
        for sub in node.get("inner", [])
    )
    if has_override_attr:
        return False

    node_slice = _extract_ast_node_slice(node, raw_text)
    if node_slice:
        sig_head = node_slice.split("{", 1)[0]
        if re.search(r"\b(override|final)\b", sig_head):
            return False
    lines = raw_text.splitlines()
    if 1 <= cur_line <= len(lines):
        if re.search(r"\b(override|final)\b", lines[cur_line - 1].split("{", 1)[0]):
            return False

    if method_name in ancestor_virtuals or bool(node.get("overrides")):
        return True
    return False


def _check_ast_naming_convention(
    node: dict[str, Any],
    naming: NamingConfig,
    access_level: str = "public",
) -> str | None:
    """
    @brief Check a declaration AST node's identifier against NamingConfig regexes.
    @param[in] node Clang AST JSON node dictionary.
    @param[in] naming NamingConfig containing category regexes.
    @param[in] access_level Current member access specifier ('public', 'protected', 'private').
    @return Error message string if a naming convention is violated, or None if valid.
    """
    if node.get("isImplicit"):
        return None
    kind = str(node.get("kind", ""))
    name = str(node.get("name", ""))
    if not name:
        return None

    if kind in ("CXXRecordDecl", "RecordDecl", "EnumDecl", "TypeAliasDecl", "TypedefDecl"):
        if naming.class_regex and not re.match(naming.class_regex, name):
            return f"Type identifier '{name}' does not match class_regex '{naming.class_regex}'"
    elif kind == "NamespaceDecl":
        if naming.namespace_regex and not re.match(naming.namespace_regex, name):
            return f"Namespace '{name}' does not match namespace_regex '{naming.namespace_regex}'"
    elif kind in ("FunctionDecl", "CXXMethodDecl"):
        if name == "main" or name.startswith("~"):
            return None
        if name.startswith("operator") and (len(name) == 8 or not name[8].isalnum()):
            return None
        if naming.function_regex and not re.match(naming.function_regex, name):
            return f"Function/method '{name}' does not match function_regex '{naming.function_regex}'"
    elif kind == "ParmVarDecl":
        if naming.parameter_regex and not re.match(naming.parameter_regex, name):
            return f"Parameter '{name}' does not match parameter_regex '{naming.parameter_regex}'"
    elif kind == "VarDecl":
        if node.get("constexpr") and naming.constant_regex:
            if not re.match(naming.constant_regex, name) and not (
                naming.variable_regex and re.match(naming.variable_regex, name)
            ):
                return f"Constant '{name}' does not match constant_regex '{naming.constant_regex}'"
        elif naming.variable_regex and not re.match(naming.variable_regex, name):
            return f"Variable '{name}' does not match variable_regex '{naming.variable_regex}'"
    elif kind == "FieldDecl":
        if access_level in ("private", "protected") and naming.private_member_regex:
            if not re.match(naming.private_member_regex, name):
                return (
                    f"Member field '{name}' ({access_level}) does not match "
                    f"private_member_regex '{naming.private_member_regex}'"
                )
        elif naming.member_regex and not re.match(naming.member_regex, name):
            return f"Member field '{name}' does not match member_regex '{naming.member_regex}'"
    return None


def _matching_ast_rules_for_node(
    node: dict[str, Any],
    raw_text: str,
    is_c: bool,
    is_header: bool,
    in_template: bool,
    max_params: int = DEFAULT_MAX_PARAMS,
    config: LintConfig | None = None,
    cur_line: int = 0,
    enclosing_class: str = "",
    has_bases: bool = False,
    ancestor_virtuals: set[str] | None = None,
    access_level: str = "public",
) -> list[str]:
    """
    @brief Determine which MISRA/AST rules trigger on a single AST node.
    @param[in] node Clang AST JSON node dictionary.
    @param[in] raw_text Full source file text.
    @param[in] is_c True when linting a C source file.
    @param[in] is_header True when linting a header file.
    @param[in] in_template True when node is inside a template declaration.
    @param[in] max_params Maximum allowed parameters for HIS-PAR.
    @param[in] config Optional LintConfig for naming/exemptions/toggles.
    @param[in] cur_line Resolved 1-based line number for node.
    @param[in] enclosing_class Name of enclosing CXXRecordDecl if any.
    @param[in] has_bases True if enclosing CXXRecordDecl has base classes.
    @param[in] ancestor_virtuals Set of virtual method names in base classes.
    @param[in] access_level Current class access specifier.
    @return List of triggered rule identifiers for this node (excluding IfStmt else-branch).
    """
    cfg = config or LintConfig(max_params=max_params)
    disabled = cfg.disabled_rules
    kind = str(node.get("kind", ""))
    triggered: list[str] = []

    if kind == "GotoStmt":
        rule = "MISRA-C-15.1" if is_c else "MISRA-9.6.1"
        if rule not in disabled:
            triggered.append(rule)
    elif kind == "SwitchStmt" and not _switch_has_default(node):
        rule = "MISRA-C-16.4" if is_c else "MISRA-9.4.2"
        if rule not in disabled:
            triggered.append(rule)
    elif kind in ("ForStmt", "WhileStmt", "CXXForRangeStmt"):
        inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
        if inner and inner[-1].get("kind") != "CompoundStmt":
            rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
            if rule not in disabled:
                triggered.append(rule)
    elif kind == "DoStmt":
        inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
        if inner and inner[0].get("kind") != "CompoundStmt":
            rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
            if rule not in disabled:
                triggered.append(rule)
    elif kind == "IfStmt":
        inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
        has_else = bool(node.get("hasElse"))
        then_idx = -2 if has_else else -1
        if len(inner) >= (2 if has_else else 1):
            then_node = inner[then_idx]
            if then_node.get("kind") != "CompoundStmt":
                rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                if rule not in disabled:
                    triggered.append(rule)
    elif kind == "VarDecl" and _is_vla_qual_type(str(node.get("type", {}).get("qualType", ""))):
        if "MISRA-C-18.8" not in disabled:
            triggered.append("MISRA-C-18.8")

    if (
        cfg.no_impl_in_header
        and "NO-IMPL-IN-HEADER" not in disabled
        and is_header
        and not in_template
        and kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl")
    ):
        if not node.get("isImplicit") and not node.get("constexpr") and _has_compound_body(node):
            triggered.append("NO-IMPL-IN-HEADER")

    if (
        "HIS-PAR" not in disabled
        and kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl")
        and not node.get("isImplicit")
    ):
        fn_name = str(node.get("name", ""))
        if not (cfg.exempt_param_prefixes and fn_name.startswith(cfg.exempt_param_prefixes)):
            param_count = sum(
                1 for c in node.get("inner", [])
                if isinstance(c, dict) and c.get("kind") == "ParmVarDecl"
            )
            if param_count > max_params:
                triggered.append("HIS-PAR")

    if (
        cfg.naming.check_meaningful
        and "NAMING-MEANINGFUL" not in disabled
        and kind in ("VarDecl", "ParmVarDecl")
        and not node.get("isImplicit")
    ):
        var_name = str(node.get("name", ""))
        if var_name:
            lower_name = var_name.lower()
            if (
                len(var_name) == 1 and var_name not in cfg.naming.allowed_loop_indices
            ) or lower_name in cfg.naming.disallowed_names:
                triggered.append("NAMING-MEANINGFUL")

    if "NAMING-CONVENTION" not in disabled:
        naming_err = _check_ast_naming_convention(node, cfg.naming, access_level=access_level)
        if naming_err is not None:
            triggered.append("NAMING-CONVENTION")

    if not is_c:
        if kind == "CStyleCastExpr" and "MISRA-8.2.2" not in disabled:
            if node.get("castKind", "") != "ToVoid":
                triggered.append("MISRA-8.2.2")
        elif kind in ("CXXNewExpr", "CXXDeleteExpr") and "MISRA-21.6.1" not in disabled:
            op_new_type = str(node.get("operatorNewDecl", {}).get("type", {}).get("qualType", ""))
            is_placement = kind == "CXXNewExpr" and "," in op_new_type
            if not is_placement:
                triggered.append("MISRA-21.6.1")
        elif (
            cfg.no_exceptions
            and "NO-EXCEPTIONS" not in disabled
            and kind in ("CXXThrowExpr", "CXXTryStmt", "CXXCatchStmt")
        ):
            triggered.append("NO-EXCEPTIONS")
        elif (
            cfg.no_rtti
            and "NO-RTTI" not in disabled
            and kind in ("CXXDynamicCastExpr", "CXXTypeidExpr")
        ):
            triggered.append("NO-RTTI")
        elif kind == "LambdaExpr" and "MISRA-LAMBDA-CAPTURE" not in disabled:
            offset = node.get("range", {}).get("begin", {}).get("offset")
            if isinstance(offset, int) and offset >= 0:
                raw_bytes = raw_text.encode("utf-8")
                if offset < len(raw_bytes):
                    snippet = raw_bytes[offset:offset + 32].decode("utf-8", errors="ignore").lstrip()
                    if re.match(r"^\[\s*[&=]\s*[,\]\)]", snippet):
                        triggered.append("MISRA-LAMBDA-CAPTURE")
        elif kind == "CXXConstructorDecl" and "MISRA-15.1.3" not in disabled:
            if _is_ctor_violation_misra_15_1_3(node, raw_text, cur_line):
                triggered.append("MISRA-15.1.3")
        elif kind == "CXXMethodDecl" and "MISRA-13.3.1" not in disabled:
            if _is_method_violation_misra_13_3_1(
                node,
                enclosing_class,
                has_bases,
                ancestor_virtuals or set(),
                raw_text,
                cur_line,
            ):
                triggered.append("MISRA-13.3.1")

    return triggered


def walk_ast_for_misra(
    root: Any,
    target_file: str,
    raw_text: str,
    findings: list[Finding],
    is_c: bool = False,
    is_header: bool = False,
    max_params: int = DEFAULT_MAX_PARAMS,
    config: LintConfig | None = None,
    plugins: Sequence[LintPlugin] = (),
) -> None:
    """Iteratively inspect clang JSON AST nodes in target_file for MISRA C/C++:2023 violations."""
    cfg = config or LintConfig(max_params=max_params)
    disabled = cfg.disabled_rules
    class_bases, class_virtuals = _collect_ast_class_hierarchy(root)
    cursor = _AstLocationCursor(target_file, raw_text)
    target_path = pathlib.Path(target_file)

    # Stack tuple: (node, in_template, parent_in_target, enclosing_class, has_bases, access_level)
    stack: list[tuple[Any, bool, bool, str, bool, str]] = [
        (root, False, False, "", False, "public")
    ]

    while stack:
        node, in_template, parent_in_target, enclosing_cls, has_bases, access_lvl = stack.pop()
        if not isinstance(node, dict):
            continue

        _, cur_line, in_target = cursor.advance_node(node, parent_in_target)
        kind = str(node.get("kind", ""))
        next_in_template = in_template or kind in ("FunctionTemplateDecl", "ClassTemplateDecl")
        next_cls = enclosing_cls
        next_has_bases = has_bases
        next_access = access_lvl

        if kind == "CXXRecordDecl" and not node.get("isImplicit"):
            cls_name = str(node.get("name", ""))
            if cls_name:
                next_cls = cls_name
                next_has_bases = bool(node.get("bases"))
                next_access = "private" if node.get("tagUsed") == "class" else "public"

        if in_target:
            if (kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl")
                    and cfg.require_param_direction
                    and "DOXYGEN-PARAM-DIR-MISMATCH" not in disabled):
                check_param_direction_types(node, raw_text, target_file, findings)
            if kind == "GotoStmt":
                rule = "MISRA-C-15.1" if is_c else "MISRA-9.6.1"
                if rule not in disabled:
                    findings.append(Finding(target_file, cur_line, rule, "error", "goto statement forbidden"))
            elif kind == "SwitchStmt" and not _switch_has_default(node):
                rule = "MISRA-C-16.4" if is_c else "MISRA-9.4.2"
                if rule not in disabled:
                    findings.append(
                        Finding(target_file, cur_line, rule, "error", "switch statement missing default clause")
                    )
            elif kind in ("ForStmt", "WhileStmt", "CXXForRangeStmt"):
                inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
                if inner and inner[-1].get("kind") != "CompoundStmt":
                    rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                    if rule not in disabled:
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                rule,
                                "error",
                                f"{kind} body must be enclosed in braces {{}}",
                            )
                        )
            elif kind == "DoStmt":
                inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
                if inner and inner[0].get("kind") != "CompoundStmt":
                    rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                    if rule not in disabled:
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                rule,
                                "error",
                                "DoStmt body must be enclosed in braces {}",
                            )
                        )
            elif kind == "IfStmt":
                inner = [c for c in node.get("inner", []) if isinstance(c, dict)]
                has_else = bool(node.get("hasElse"))
                then_idx = -2 if has_else else -1
                if len(inner) >= (2 if has_else else 1):
                    then_node = inner[then_idx]
                    if then_node.get("kind") != "CompoundStmt":
                        rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                        if rule not in disabled:
                            findings.append(
                                Finding(
                                    target_file,
                                    cur_line,
                                    rule,
                                    "error",
                                    "if-branch body must be enclosed in braces {}",
                                )
                            )
                    if has_else:
                        else_node = inner[-1]
                        if else_node.get("kind") not in ("CompoundStmt", "IfStmt"):
                            else_line = cursor.peek_node_line(else_node, cur_line)
                            rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                            if rule not in disabled:
                                findings.append(
                                    Finding(
                                        target_file,
                                        else_line,
                                        rule,
                                        "error",
                                        "else-branch body must be enclosed in braces {}",
                                    )
                                )
            elif kind == "VarDecl" and _is_vla_qual_type(str(node.get("type", {}).get("qualType", ""))):
                if "MISRA-C-18.8" not in disabled:
                    findings.append(
                        Finding(target_file, cur_line, "MISRA-C-18.8", "error", "Variable Length Array (VLA) forbidden")
                    )

            if (
                cfg.no_impl_in_header
                and "NO-IMPL-IN-HEADER" not in disabled
                and is_header
                and not next_in_template
                and kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl")
            ):
                if not node.get("isImplicit") and not node.get("constexpr") and _has_compound_body(node):
                    fn_name = node.get("name", "<anonymous>")
                    findings.append(
                        Finding(
                            target_file,
                            cur_line,
                            "NO-IMPL-IN-HEADER",
                            "error",
                            f"Function/method '{fn_name}' implemented in header file; move implementation to .cpp/.c",
                        )
                    )

            if (
                "HIS-PAR" not in disabled
                and kind in ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl")
                and not node.get("isImplicit")
            ):
                fn_name = str(node.get("name", ""))
                if not (cfg.exempt_param_prefixes and fn_name.startswith(cfg.exempt_param_prefixes)):
                    param_count = sum(
                        1 for c in node.get("inner", [])
                        if isinstance(c, dict) and c.get("kind") == "ParmVarDecl"
                    )
                    if param_count > max_params:
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "HIS-PAR",
                                "error",
                                f"Function '{fn_name}' has {param_count} parameters (max {max_params}); "
                                "split responsibility or group by reusable domain type",
                            )
                        )

            if (
                cfg.naming.check_meaningful
                and "NAMING-MEANINGFUL" not in disabled
                and kind in ("VarDecl", "ParmVarDecl")
                and not node.get("isImplicit")
            ):
                var_name = str(node.get("name", ""))
                if var_name:
                    lower_name = var_name.lower()
                    if (
                        len(var_name) == 1 and var_name not in cfg.naming.allowed_loop_indices
                    ) or lower_name in cfg.naming.disallowed_names:
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "NAMING-MEANINGFUL",
                                "error",
                                f"Non-meaningful identifier '{var_name}'; use a descriptive domain name",
                            )
                        )

            if "NAMING-CONVENTION" not in disabled:
                naming_err = _check_ast_naming_convention(node, cfg.naming, access_level=access_lvl)
                if naming_err is not None:
                    findings.append(
                        Finding(target_file, cur_line, "NAMING-CONVENTION", "error", naming_err)
                    )

            if not is_c:
                if kind == "CStyleCastExpr" and "MISRA-8.2.2" not in disabled:
                    if node.get("castKind", "") != "ToVoid":
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "MISRA-8.2.2",
                                "error",
                                "C-style cast forbidden; use static_cast/const_cast/reinterpret_cast",
                            )
                        )
                elif kind in ("CXXNewExpr", "CXXDeleteExpr") and "MISRA-21.6.1" not in disabled:
                    op_new_type = str(node.get("operatorNewDecl", {}).get("type", {}).get("qualType", ""))
                    is_placement = kind == "CXXNewExpr" and "," in op_new_type
                    if not is_placement:
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "MISRA-21.6.1",
                                "error",
                                f"Dynamic heap allocation ({kind}) forbidden outside allowed paths",
                            )
                        )
                elif (
                    cfg.no_exceptions
                    and "NO-EXCEPTIONS" not in disabled
                    and kind in ("CXXThrowExpr", "CXXTryStmt", "CXXCatchStmt")
                ):
                    findings.append(
                        Finding(
                            target_file,
                            cur_line,
                            "NO-EXCEPTIONS",
                            "error",
                            f"C++ exceptions ({kind}) forbidden under -fno-exceptions",
                        )
                    )
                elif (
                    cfg.no_rtti
                    and "NO-RTTI" not in disabled
                    and kind in ("CXXDynamicCastExpr", "CXXTypeidExpr")
                ):
                    findings.append(
                        Finding(
                            target_file,
                            cur_line,
                            "NO-RTTI",
                            "error",
                            f"RTTI construct ({kind}) forbidden under -fno-rtti",
                        )
                    )
                elif kind == "LambdaExpr" and "MISRA-LAMBDA-CAPTURE" not in disabled:
                    offset = node.get("range", {}).get("begin", {}).get("offset")
                    if isinstance(offset, int) and offset >= 0:
                        raw_bytes = raw_text.encode("utf-8")
                        if offset < len(raw_bytes):
                            snippet = raw_bytes[offset:offset + 32].decode("utf-8", errors="ignore").lstrip()
                            if re.match(r"^\[\s*[&=]\s*[,\]\)]", snippet):
                                findings.append(
                                    Finding(
                                        target_file,
                                        cur_line,
                                        "MISRA-LAMBDA-CAPTURE",
                                        "error",
                                        "Implicit lambda capture ([&] or [=]) forbidden; list captures explicitly",
                                    )
                                )
                elif kind == "CXXConstructorDecl" and "MISRA-15.1.3" not in disabled:
                    if _is_ctor_violation_misra_15_1_3(node, raw_text, cur_line):
                        ctor_name = str(node.get("name", "<ctor>"))
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "MISRA-15.1.3",
                                "error",
                                f"Single-argument constructor '{ctor_name}' must be marked explicit",
                            )
                        )
                elif kind == "CXXMethodDecl" and "MISRA-13.3.1" not in disabled:
                    anc_virtuals = _get_ancestor_virtual_methods(enclosing_cls, class_bases, class_virtuals)
                    if _is_method_violation_misra_13_3_1(
                        node,
                        enclosing_cls,
                        has_bases,
                        anc_virtuals,
                        raw_text,
                        cur_line,
                    ):
                        m_name = str(node.get("name", "<method>"))
                        findings.append(
                            Finding(
                                target_file,
                                cur_line,
                                "MISRA-13.3.1",
                                "error",
                                f"Overriding virtual method '{m_name}' must be marked 'override' or 'final'",
                            )
                        )

            for plugin in plugins:
                findings.extend(plugin.check_ast_node(node, target_path, cur_line, raw_text, cfg))

        children = node.get("inner", [])
        if kind == "CXXRecordDecl" and isinstance(children, list):
            # Maintain access specifier across record children in forward order
            cur_acc = next_access
            forward_entries: list[tuple[Any, bool, bool, str, bool, str]] = []
            for child in children:
                if isinstance(child, dict) and child.get("kind") == "AccessSpecDecl":
                    cur_acc = str(child.get("access", cur_acc))
                forward_entries.append((child, next_in_template, in_target, next_cls, next_has_bases, cur_acc))
            for entry in reversed(forward_entries):
                stack.append(entry)
        else:
            for child in reversed(children):
                stack.append((child, next_in_template, in_target, next_cls, next_has_bases, next_access))


def _extract_statement_tokens(raw_text: str, line_number: int) -> tuple[str, ...]:
    """
    @brief Extract normalized C/C++ tokens for the logical statement containing line_number.
    @param[in] raw_text Full source file text.
    @param[in] line_number 1-based physical line number.
    @return Tuple of tokens in the enclosing statement or directive.
    """
    if line_number <= 0 or not raw_text:
        return ()
    lines = raw_text.splitlines(keepends=True)
    if line_number > len(lines):
        return ()
    line_start = sum(len(lines[index]) for index in range(line_number - 1))
    target_line = lines[line_number - 1].lstrip()
    if target_line.startswith("#"):
        return tokenize_cpp_snippet(lines[line_number - 1])

    start_pos = line_start
    while start_pos > 0:
        prev_char = raw_text[start_pos - 1]
        if prev_char in ";{}":
            break
        start_pos -= 1

    end_pos = line_start
    text_len = len(raw_text)
    while end_pos < text_len:
        cur_char = raw_text[end_pos]
        if cur_char in ";{}":
            if cur_char == ";":
                end_pos += 1
            break
        end_pos += 1

    return tokenize_cpp_snippet(raw_text[start_pos:end_pos])


def _parse_compiler_diagnostic_location(out_line: str) -> tuple[str, int]:
    """
    @brief Parse file path and line number from a clang or clang-tidy diagnostic line.
    @param[in] out_line Single diagnostic output line.
    @return Tuple of (diagnostic_file_path, line_number), or ("", 0) when unavailable.
    """
    match = re.match(r"^(.+?):(\d+)(?::\d+)?:\s*(?:fatal\s+)?(?:error|warning):", out_line.strip())
    if match is None:
        return "", 0
    return match.group(1), int(match.group(2))


def _attach_ast_contexts(
    ast_root: Any,
    target_path: pathlib.Path,
    raw_text: str,
    ast_findings: list[Finding],
    contexts: dict[int, FingerprintContext] | None,
    is_c: bool = False,
    is_header: bool = False,
    max_params: int = DEFAULT_MAX_PARAMS,
    config: LintConfig | None = None,
) -> None:
    """
    @brief Populate FingerprintContext entries for AST findings using node ranges and enclosing symbols.
    @param[in] ast_root Parsed Clang AST JSON root object.
    @param[in] target_path Source file path being checked.
    @param[in] raw_text Full source file text.
    @param[in] ast_findings Findings produced by walk_ast_for_misra.
    @param[in,out] contexts Optional mapping from Finding id to FingerprintContext.
    @param[in] is_c True when linting a C source file.
    @param[in] is_header True when linting a header file.
    @param[in] max_params Maximum allowed parameters for HIS-PAR.
    @param[in] config Optional LintConfig.
    @return None.
    """
    if contexts is None or not ast_findings:
        return

    cfg = config or LintConfig(max_params=max_params)
    class_bases, class_virtuals = _collect_ast_class_hierarchy(ast_root)
    cursor = _AstLocationCursor(target_path, raw_text)
    context_buckets: dict[tuple[int, str], list[FingerprintContext]] = {}
    scope_kinds = (
        "NamespaceDecl",
        "CXXRecordDecl",
        "RecordDecl",
        "FunctionDecl",
        "CXXMethodDecl",
        "CXXConstructorDecl",
        "CXXDestructorDecl",
    )
    fn_scope_kinds = (
        "FunctionDecl",
        "CXXMethodDecl",
        "CXXConstructorDecl",
        "CXXDestructorDecl",
    )
    stack: list[tuple[Any, tuple[str, ...], bool, bool, str, bool, str]] = [
        (ast_root, (), False, False, "", False, "public")
    ]

    while stack:
        node, scope_chain, in_template, parent_in_target, enclosing_cls, has_bases, access_lvl = stack.pop()
        if not isinstance(node, dict):
            continue

        _, cur_line, in_target = cursor.advance_node(node, parent_in_target)
        kind = str(node.get("kind", ""))
        next_in_template = in_template or kind in ("FunctionTemplateDecl", "ClassTemplateDecl")
        next_scope = scope_chain
        if kind in scope_kinds:
            node_name = str(node.get("name", ""))
            if node_name:
                next_scope = (*scope_chain, node_name)

        next_cls = enclosing_cls
        next_has_bases = has_bases
        next_access = access_lvl
        if kind == "CXXRecordDecl" and not node.get("isImplicit"):
            cls_name = str(node.get("name", ""))
            if cls_name:
                next_cls = cls_name
                next_has_bases = bool(node.get("bases"))
                next_access = "private" if node.get("tagUsed") == "class" else "public"

        if in_target:
            anc_virtuals = _get_ancestor_virtual_methods(enclosing_cls, class_bases, class_virtuals)
            rules = _matching_ast_rules_for_node(
                node,
                raw_text,
                is_c=is_c,
                is_header=is_header,
                in_template=next_in_template,
                max_params=max_params,
                config=cfg,
                cur_line=cur_line,
                enclosing_class=enclosing_cls,
                has_bases=has_bases,
                ancestor_virtuals=anc_virtuals,
                access_level=access_lvl,
            )
            if rules:
                node_slice = _extract_ast_node_slice(node, raw_text)
                enclosing = "::".join(next_scope) if kind in fn_scope_kinds else "::".join(scope_chain)
                for rule_id in rules:
                    rule_slice = node_slice
                    if rule_id in ("HIS-PAR", "MISRA-15.1.3", "MISRA-13.3.1", "NAMING-CONVENTION") and "{" in rule_slice:
                        rule_slice = rule_slice.split("{", 1)[0]
                    tokens = (
                        tokenize_cpp_snippet(rule_slice)
                        if rule_slice
                        else _extract_statement_tokens(raw_text, cur_line)
                    )
                    ctx = FingerprintContext(enclosing_symbol=enclosing, tokens=tokens)
                    context_buckets.setdefault((cur_line, rule_id), []).append(ctx)

            if kind == "IfStmt" and node.get("hasElse"):
                inner_nodes = [item for item in node.get("inner", []) if isinstance(item, dict)]
                if len(inner_nodes) >= 2:
                    else_node = inner_nodes[-1]
                    if else_node.get("kind") not in ("CompoundStmt", "IfStmt"):
                        else_line = cursor.peek_node_line(else_node, cur_line)
                        else_slice = _extract_ast_node_slice(else_node, raw_text)
                        else_tokens = (
                            tokenize_cpp_snippet(else_slice)
                            if else_slice
                            else _extract_statement_tokens(raw_text, else_line)
                        )
                        else_ctx = FingerprintContext(enclosing_symbol="::".join(scope_chain), tokens=else_tokens)
                        else_rule = "MISRA-C-15.6" if is_c else "MISRA-9.3.1"
                        context_buckets.setdefault((else_line, else_rule), []).append(else_ctx)

        children = node.get("inner", [])
        if kind == "CXXRecordDecl" and isinstance(children, list):
            cur_acc = next_access
            forward_entries: list[tuple[Any, tuple[str, ...], bool, bool, str, bool, str]] = []
            for child in children:
                if isinstance(child, dict) and child.get("kind") == "AccessSpecDecl":
                    cur_acc = str(child.get("access", cur_acc))
                forward_entries.append(
                    (child, next_scope, next_in_template, in_target, next_cls, next_has_bases, cur_acc)
                )
            for entry in reversed(forward_entries):
                stack.append(entry)
        else:
            for child in reversed(children):
                stack.append(
                    (child, next_scope, next_in_template, in_target, next_cls, next_has_bases, next_access)
                )

    consumed_indices: dict[tuple[int, str], int] = {}
    for finding in ast_findings:
        bucket_key = (finding.line, finding.rule)
        candidates = context_buckets.get(bucket_key, [])
        offset_idx = consumed_indices.get(bucket_key, 0)
        if offset_idx < len(candidates):
            contexts[id(finding)] = candidates[offset_idx]
            consumed_indices[bucket_key] = offset_idx + 1
        else:
            fallback_tokens = _extract_statement_tokens(raw_text, finding.line)
            contexts[id(finding)] = FingerprintContext(tokens=fallback_tokens)


def _split_top_level_params(param_text: str) -> list[str]:
    """Split a C/C++ parameter list string by top-level commas."""
    stripped = param_text.strip()
    if not stripped or stripped == "void":
        return []
    parts: list[str] = []
    cur: list[str] = []
    depth_angle = 0
    depth_paren = 0
    for ch in stripped:
        if ch == "<":
            depth_angle += 1
        elif ch == ">" and depth_angle > 0:
            depth_angle -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif ch == "," and depth_angle == 0 and depth_paren == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _run_structural_lexical_checks(
    stripped_text: str,
    target_file: str,
    is_c: bool,
    heap_allowed: bool,
    config: LintConfig,
    include_ast_fallback: bool,
    existing_rule_lines: set[tuple[str, int]],
    add_finding_cb: Any,
) -> None:
    """
    @brief Run lexical checks for C/C++ constructs and AST fallback when AST is unavailable.
    @param[in] stripped_text Comment-and-string-stripped source code.
    @param[in] target_file File path string.
    @param[in] is_c True for C files, False for C++ files.
    @param[in] heap_allowed True if dynamic memory allocation is allowed in this file.
    @param[in] config Active LintConfig.
    @param[in] include_ast_fallback True when Clang AST did not run.
    @param[in,out] existing_rule_lines Set of (rule, line) pairs already emitted.
    @param[in] add_finding_cb Callback(line, rule, severity, message) to record findings.
    @return None.
    """
    disabled = config.disabled_rules
    lines = stripped_text.splitlines()

    def _emit_once(line_num: int, rule_id: str, message: str) -> None:
        """Emit a finding if not disabled and not already reported on line_num."""
        if rule_id in disabled or (rule_id, line_num) in existing_rule_lines:
            return
        existing_rule_lines.add((rule_id, line_num))
        add_finding_cb(line_num, rule_id, "error", message)

    # 1. Implicit lambda captures [&] or [=] (C++ only)
    if not is_c and "MISRA-LAMBDA-CAPTURE" not in disabled:
        for idx, line_text in enumerate(lines, start=1):
            if IMPLICIT_LAMBDA_PATTERN.search(line_text):
                _emit_once(
                    idx,
                    "MISRA-LAMBDA-CAPTURE",
                    "Implicit lambda capture ([&] or [=]) forbidden; list captures explicitly",
                )

    # 2. Switch default check (balanced braces)
    switch_rule = "MISRA-C-16.4" if is_c else "MISRA-9.4.2"
    if switch_rule not in disabled and include_ast_fallback:
        line_starts = _build_line_start_offsets(stripped_text)
        for sw_match in re.finditer(r"\bswitch\s*\([^)]*\)\s*\{", stripped_text):
            sw_line = _offset_to_line(line_starts, len(stripped_text[:sw_match.start()].encode("utf-8")))
            brace_start = sw_match.end() - 1
            depth = 0
            pos = brace_start
            top_level_chunks: list[str] = []
            chunk_start = brace_start + 1
            while pos < len(stripped_text):
                ch = stripped_text[pos]
                if ch == "{":
                    if depth == 1:
                        top_level_chunks.append(stripped_text[chunk_start:pos])
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 1:
                        chunk_start = pos + 1
                    elif depth == 0:
                        top_level_chunks.append(stripped_text[chunk_start:pos])
                        break
                pos += 1
            outer_body = " ".join(top_level_chunks)
            if not re.search(r"\bdefault\s*:", outer_body) and not re.search(r"\bdefault\s*:", stripped_text[brace_start:pos]):
                _emit_once(sw_line, switch_rule, "switch statement missing default clause")

    # 3. Class-based checks for C++: explicit single-arg ctors (MISRA-15.1.3) and override (MISRA-13.3.1)
    if not is_c and include_ast_fallback:
        class_virtuals: dict[str, set[str]] = {}
        class_spans: list[tuple[str, list[str], int, int]] = []
        line_starts = _build_line_start_offsets(stripped_text)
        cls_pattern = re.compile(
            r"\b(class|struct)\s+([A-Za-z_]\w*)\s*(?::\s*([^{;]+))?\{",
            re.MULTILINE,
        )
        for m_cls in cls_pattern.finditer(stripped_text):
            cls_name = m_cls.group(2)
            bases_raw = m_cls.group(3) or ""
            bases: list[str] = []
            for b_part in bases_raw.split(","):
                b_clean = re.sub(r"\b(public|protected|private|virtual)\b", "", b_part).strip()
                b_clean = _clean_base_class_name(b_clean)
                if b_clean:
                    bases.append(b_clean)
            brace_open = m_cls.end() - 1
            depth = 0
            pos = brace_open
            while pos < len(stripped_text):
                if stripped_text[pos] == "{":
                    depth += 1
                elif stripped_text[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                pos += 1
            start_line = _offset_to_line(line_starts, len(stripped_text[:m_cls.start()].encode("utf-8")))
            end_line = _offset_to_line(line_starts, len(stripped_text[:pos].encode("utf-8")))
            class_spans.append((cls_name, bases, start_line, end_line))
            v_set = class_virtuals.setdefault(cls_name, set())
            for l_idx in range(start_line, min(end_line + 1, len(lines) + 1)):
                l_txt = lines[l_idx - 1]
                m_virt = re.search(r"\bvirtual\b[^;{}]*?\b([A-Za-z_]\w*)\s*\(", l_txt)
                if m_virt:
                    v_set.add(m_virt.group(1))

        for cls_name, bases, start_line, end_line in class_spans:
            inherited_virtuals: set[str] = set()
            for b_name in bases:
                inherited_virtuals.update(class_virtuals.get(b_name, ()))
            for l_idx in range(start_line, min(end_line + 1, len(lines) + 1)):
                l_txt = lines[l_idx - 1].strip()
                if not l_txt or l_txt.startswith(("~", "//")):
                    continue
                # Check constructor
                m_ctor = re.match(
                    rf"^(?:(explicit|inline|constexpr)\s+)*{re.escape(cls_name)}\s*\(([^)]*)\)\s*(.*)",
                    l_txt,
                )
                if m_ctor:
                    prefix_text = l_txt.split("(", 1)[0]
                    is_expl = bool(re.search(r"\bexplicit\b", prefix_text))
                    params_str = m_ctor.group(2)
                    tail = m_ctor.group(3)
                    if not re.search(r"=\s*(default|delete)\b", tail) and not is_expl:
                        param_items = _split_top_level_params(params_str)
                        req_items = [p for p in param_items if "=" not in p]
                        if len(param_items) >= 1 and len(req_items) <= 1:
                            first_p = param_items[0]
                            is_copy_move = bool(
                                re.search(
                                    rf"\b(?:const\s+)?{re.escape(cls_name)}\b\s*&&?",
                                    first_p,
                                )
                            )
                            if not is_copy_move:
                                _emit_once(
                                    l_idx,
                                    "MISRA-15.1.3",
                                    f"Single-argument constructor '{cls_name}' must be marked explicit",
                                )
                    continue

                # Check overriding method without override/final
                if bases:
                    m_meth = re.match(
                        r"^(?:virtual\s+|inline\s+|constexpr\s+)*[A-Za-z_][\w:<>,\s*&]*?\b([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:const\s*)?(?:noexcept\s*)?([^{;]*)",
                        l_txt,
                    )
                    if m_meth:
                        m_name = m_meth.group(1)
                        qual_tail = m_meth.group(2) or ""
                        if (
                            m_name != cls_name
                            and m_name not in ("if", "for", "while", "switch", "return")
                            and not re.search(r"\b(override|final)\b", qual_tail)
                            and not re.search(r"\b(override|final)\b", l_txt.split("{", 1)[0])
                        ):
                            if m_name in inherited_virtuals or (
                                l_txt.startswith("virtual ") and "= 0" not in l_txt
                            ):
                                _emit_once(
                                    l_idx,
                                    "MISRA-13.3.1",
                                    f"Overriding virtual method '{m_name}' must be marked 'override' or 'final'",
                                )

    if not include_ast_fallback:
        return

    # 4. Additional lexical fallbacks when Clang AST is unavailable
    for idx, line_text in enumerate(lines, start=1):
        if not is_c and "MISRA-8.2.2" not in disabled:
            if C_STYLE_CAST_LINE_PATTERN.search(line_text):
                _emit_once(
                    idx,
                    "MISRA-8.2.2",
                    "C-style cast forbidden; use static_cast/const_cast/reinterpret_cast",
                )
        if not is_c and not heap_allowed and "MISRA-21.6.1" not in disabled:
            if re.search(r"(?<![A-Za-z0-9_])new\s+(?!\()[A-Za-z_]", line_text) and not re.search(
                r"\boperator\s+new\b", line_text
            ):
                _emit_once(
                    idx,
                    "MISRA-21.6.1",
                    "Dynamic heap allocation (CXXNewExpr) forbidden outside allowed paths",
                )
            elif re.search(r"(?<![=\s])\bdelete\b(?:\s*\[\s*\])?\s+[A-Za-z_(*]", line_text) and not re.search(
                r"=\s*delete\b|\boperator\s+delete\b", line_text
            ):
                _emit_once(
                    idx,
                    "MISRA-21.6.1",
                    "Dynamic heap allocation (CXXDeleteExpr) forbidden outside allowed paths",
                )
        if re.search(r"\bgoto\s+[A-Za-z_]\w*\s*;", line_text):
            _emit_once(idx, "MISRA-C-15.1" if is_c else "MISRA-9.6.1", "goto statement forbidden")
        if not is_c and config.no_exceptions and "NO-EXCEPTIONS" not in disabled:
            if re.search(r"\b(throw|try|catch)\b", line_text):
                _emit_once(idx, "NO-EXCEPTIONS", "C++ exceptions forbidden under -fno-exceptions")
        if not is_c and config.no_rtti and "NO-RTTI" not in disabled:
            if re.search(r"\b(dynamic_cast|typeid)\b", line_text):
                _emit_once(idx, "NO-RTTI", "RTTI construct forbidden under -fno-rtti")

        # Parameter count and naming fallback on function definitions/declarations
        m_fn = re.match(
            r"^\s*(?:(?:static|inline|virtual|constexpr|explicit|extern)\s+)*"
            r"[A-Za-z_][\w:<>,\s*&]+?\b([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*|final\s*)?(?:\{|;)",
            line_text,
        )
        if m_fn:
            fn_name = m_fn.group(1)
            if fn_name not in ("if", "for", "while", "switch", "return", "catch"):
                params_list = _split_top_level_params(m_fn.group(2))
                if (
                    "HIS-PAR" not in disabled
                    and not (config.exempt_param_prefixes and fn_name.startswith(config.exempt_param_prefixes))
                    and len(params_list) > config.max_params
                ):
                    _emit_once(
                        idx,
                        "HIS-PAR",
                        f"Function '{fn_name}' has {len(params_list)} parameters (max {config.max_params}); "
                        "split responsibility or group by reusable domain type",
                    )
                if (
                    "NAMING-CONVENTION" not in disabled
                    and fn_name != "main"
                    and config.naming.function_regex
                    and not re.match(config.naming.function_regex, fn_name)
                ):
                    _emit_once(
                        idx,
                        "NAMING-CONVENTION",
                        f"Function/method '{fn_name}' does not match function_regex '{config.naming.function_regex}'",
                    )
                for p_item in params_list:
                    p_no_default = p_item.split("=", 1)[0].strip()
                    m_pname = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", p_no_default)
                    if m_pname:
                        p_name = m_pname.group(1)
                        if p_name not in ("int", "void", "char", "double", "float", "bool", "long", "short"):
                            if config.naming.check_meaningful and "NAMING-MEANINGFUL" not in disabled:
                                if (
                                    len(p_name) == 1 and p_name not in config.naming.allowed_loop_indices
                                ) or p_name.lower() in config.naming.disallowed_names:
                                    _emit_once(
                                        idx,
                                        "NAMING-MEANINGFUL",
                                        f"Non-meaningful identifier '{p_name}'; use a descriptive domain name",
                                    )
                            if (
                                "NAMING-CONVENTION" not in disabled
                                and config.naming.parameter_regex
                                and not re.match(config.naming.parameter_regex, p_name)
                            ):
                                _emit_once(
                                    idx,
                                    "NAMING-CONVENTION",
                                    f"Parameter '{p_name}' does not match parameter_regex '{config.naming.parameter_regex}'",
                                )


_PROCESS_TERMINATION_CALL_RE = re.compile(
    r"(?<![.\w>])(?:std::)?(?:_Exit|quick_exit|exit|abort|atexit|at_quick_exit"
    r"|system|popen|fork|exec(?:l|le|lp|v|ve|vp|vpe))\s*\("
)
_PROCESS_TERMINATION_IDENT_RE = re.compile(
    r"(?<![.\w>])(?:std::)?(?:_Exit|quick_exit|atexit|at_quick_exit)\b|\bstd::terminate\b"
)
_ATTR_CTOR_DTOR_RE = re.compile(r"__attribute__\s*\(\s*\(\s*(?:constructor|destructor)\b")
_LOCAL_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"')


def _is_test_code_path(path: pathlib.Path, repository_root: pathlib.Path | None) -> bool:
    """Return True when ``path`` sits in a unit/hidden test directory rather than worker code."""
    try:
        rel = path.resolve().relative_to(repository_root.resolve()) if repository_root else path
        parts = rel.parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    return any(p in ("tests", "test", "hidden", "hidden_tests") for p in parts)


def _line_has_process_termination(line_text: str) -> bool:
    """Return True if a comment/string-stripped line invokes process-termination/exec/ctor primitives."""
    return bool(
        _PROCESS_TERMINATION_CALL_RE.search(line_text)
        or _PROCESS_TERMINATION_IDENT_RE.search(line_text)
        or _ATTR_CTOR_DTOR_RE.search(line_text)
    )


def _has_process_termination_deviation(
    path: pathlib.Path,
    repository_root: pathlib.Path | None,
    deviation_id: str,
) -> bool:
    """Return True only if ``deviation_id`` validates in DEVPACK_DEVIATIONS for this file."""
    clean_id = (deviation_id or "").strip()
    if not clean_id:
        return False
    rel_file = str(path.name)
    if repository_root is not None:
        try:
            rel_file = path.resolve().relative_to(repository_root.resolve()).as_posix()
        except ValueError:
            rel_file = path.as_posix()
    roots = (repository_root,) if repository_root is not None else ()
    entry, _reason = verify_deviation(
        clean_id,
        files=(rel_file,),
        rule="PROCESS-TERMINATION",
        worktree_roots=roots,
    )
    return entry is not None


def check_c_or_cpp_file(
    path: pathlib.Path,
    compile_commands: pathlib.Path | None,
    clang_tidy_cfg: pathlib.Path | None,
    std: str,
    allow_new_in_init: bool,
    max_params: int = DEFAULT_MAX_PARAMS,
    contexts: dict[int, FingerprintContext] | None = None,
    skip_clang_tidy: bool = False,
    tool_env: RuntimeToolEnv | None = None,
    config: LintConfig | None = None,
    repository_root: pathlib.Path | None = None,
    plugins: Sequence[LintPlugin] = (),
) -> FileReport:
    """Run Doxygen, lexical, compiler syntax, Clang AST MISRA, clang-tidy, and plugin checks on a C/C++ file."""
    cfg = config or LintConfig(
        std=std,
        max_params=max_params,
        allow_new_in_init=allow_new_in_init,
        skip_clang_tidy=skip_clang_tidy,
    )
    disabled = cfg.disabled_rules
    report = FileReport(str(path))
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    stripped = strip_comments_and_strings(raw_text)
    is_c = path.suffix in C_EXTS
    is_header = path.suffix in HEADER_EXTS
    extra_roots = (str(compile_commands.parent),) if compile_commands is not None else ()
    heap_allowed = is_heap_allowed_for_path(
        path,
        repository_root,
        cfg.allow_new_in_init or allow_new_in_init,
        cfg.allowed_heap_paths,
    )

    def _add_lexical_finding(line_num: int, rule_id: str, severity: str, message: str) -> None:
        """Record a lexical finding and attach statement token context for fingerprinting."""
        if rule_id in disabled:
            return
        finding = Finding(str(path), line_num, rule_id, severity, message)
        report.findings.append(finding)
        if contexts is not None:
            contexts[id(finding)] = FingerprintContext(
                tokens=_extract_statement_tokens(raw_text, line_num),
                extra_roots=extra_roots,
            )

    def _add_tool_missing_finding(message: str) -> None:
        """Record a TOOL-MISSING finding when AST or compile info is unavailable."""
        severity = "warning" if cfg.allow_missing_tools else "error"
        finding = Finding(str(path), 1, "TOOL-MISSING", severity, message)
        report.findings.append(finding)
        if contexts is not None:
            contexts[id(finding)] = FingerprintContext(
                tokens=("TOOL-MISSING", path.suffix),
                extra_roots=extra_roots,
            )

    # Check compile_commands.json availability / entry when --compile-commands is provided
    if compile_commands is not None:
        if not compile_commands.is_file():
            _add_tool_missing_finding(f"compile database file not found: {compile_commands}")
        else:
            entries = (
                tool_env.compile_commands_entries
                if (tool_env is not None and tool_env.compile_commands_entries)
                else ()
            )
            if not entries:
                try:
                    parsed_db = json.loads(compile_commands.read_text(encoding="utf-8"))
                    if isinstance(parsed_db, list):
                        entries = tuple(item for item in parsed_db if isinstance(item, dict))
                except (OSError, json.JSONDecodeError):
                    entries = ()
            if not entries:
                _add_tool_missing_finding(f"compile database is empty or invalid: {compile_commands}")
            elif not is_header and not _has_compile_database_entry(entries, compile_commands.parent, path):
                _add_tool_missing_finding(
                    f"no compile-database entry found for source file '{path.name}'"
                )

    # 1. Doxygen header, @param direction, and comment hygiene checks
    if cfg.require_file_doxygen and "DOXYGEN-FILE" not in disabled:
        has_file_tag = ("@file" in raw_text or "\\file" in raw_text)
        has_brief_tag = ("@brief" in raw_text or "\\brief" in raw_text)
        if not has_file_tag or not has_brief_tag:
            dox_finding = Finding(
                str(path),
                1,
                "DOXYGEN-FILE",
                "error",
                "Missing Doxygen @file and/or @brief file header",
            )
            report.findings.append(dox_finding)
            if contexts is not None:
                contexts[id(dox_finding)] = FingerprintContext(extra_roots=extra_roots)

    check_comments_and_doxygen(
        raw_text,
        str(path),
        report.findings,
        contexts=contexts,
        require_param_direction=cfg.require_param_direction,
        disabled_rules=disabled,
    )

    # 2. Lexical MISRA C & C++ checks on comment-stripped text
    for idx, line_text in enumerate(stripped.splitlines(), start=1):
        if not heap_allowed and re.search(r"(?<![.\w>])\b(malloc|calloc|realloc|free)\s*\(", line_text):
            rule = "MISRA-C-21.3" if is_c else "MISRA-21.6.1"
            _add_lexical_finding(
                idx,
                rule,
                "error",
                "C heap allocation (malloc/calloc/realloc/free) forbidden outside allowed paths",
            )
        if (
            re.search(r"(?<![.\w>:])\b(setjmp|longjmp)\s*\(", line_text)
            or re.search(r"^\s*#\s*include\s*<(?:setjmp\.h|csetjmp)>", line_text)
        ):
            rule = "MISRA-C-21.4" if is_c else "MISRA-21.10.2"
            _add_lexical_finding(idx, rule, "error", "<setjmp.h> / setjmp / longjmp forbidden")
        if re.search(r"(?<![.\w>:])\b(abort|exit|_Exit|quick_exit|system)\s*\(", line_text):
            rule = "MISRA-C-21.8" if is_c else "MISRA-18.5.2"
            _add_lexical_finding(
                idx,
                rule,
                "error",
                "Process-terminating / shell functions (abort/exit/system) forbidden",
            )
        if not _is_test_code_path(path, repository_root) and _line_has_process_termination(line_text):
            if not _has_process_termination_deviation(path, repository_root, cfg.deviation_id):
                finding = Finding(
                    str(path),
                    idx,
                    "PROCESS-TERMINATION",
                    "error",
                    "Process-terminating, exec/spawn, or constructor/destructor attribute forbidden in worker code",
                )
                report.findings.append(finding)
                if contexts is not None:
                    contexts[id(finding)] = FingerprintContext(
                        tokens=_extract_statement_tokens(raw_text, idx),
                        extra_roots=extra_roots,
                    )
        if re.search(r"(?<![.\w>:])\b(strcpy|strcat|sprintf|vsprintf|gets)\s*\(", line_text):
            rule = "MISRA-C-21.17" if is_c else "MISRA-STR-BOUND"
            _add_lexical_finding(
                idx,
                rule,
                "error",
                "Unbounded C string function (strcpy/strcat/sprintf/vsprintf/gets) forbidden; use snprintf/bounded copy",
            )
        if re.search(
            r"^\s*#\s*define\s+(const|inline|volatile|static|true|false|bool|if|else|while|for|return)\b",
            line_text,
        ):
            _add_lexical_finding(idx, "MISRA-C-20.4", "error", "Macro shall not redefine a C/C++ keyword")

    # Scan non-standard locally #included fragments (e.g. .inc/.inl) within repo for PROCESS-TERMINATION
    if not _is_test_code_path(path, repository_root):
        for idx, raw_line in enumerate(raw_text.splitlines(), start=1):
            inc_match = _LOCAL_INCLUDE_RE.match(raw_line)
            if not inc_match:
                continue
            inc_rel = inc_match.group(1)
            for base_dir in (path.parent, *( [repository_root / "src", repository_root / "include"] if repository_root else [] )):
                cand = (base_dir / inc_rel).resolve()
                if cand.is_file() and cand.suffix not in (C_EXTS | CPP_EXTS):
                    if repository_root is not None:
                        try:
                            cand.relative_to(repository_root.resolve())
                        except ValueError:
                            continue
                    inc_stripped = strip_comments_and_strings(cand.read_text(encoding="utf-8", errors="replace"))
                    for inc_ln, inc_text in enumerate(inc_stripped.splitlines(), start=1):
                        if _line_has_process_termination(inc_text) and not _has_process_termination_deviation(
                            cand, repository_root, cfg.deviation_id
                        ):
                            finding = Finding(
                                str(cand),
                                inc_ln,
                                "PROCESS-TERMINATION",
                                "error",
                                f"Process-terminating or constructor/destructor attribute forbidden in included fragment (from {path.name}:{idx})",
                            )
                            report.findings.append(finding)
                            break
                    break

    # 3. Compiler syntax & warning check + 4. Clang AST JSON check
    if tool_env is not None:
        compiler_bin = (tool_env.clang_bin if is_c else tool_env.clangxx_bin) or None
        gcc_dir = tool_env.gcc_dir or None
    else:
        compiler_bin = shutil.which("clang" if is_c else "clang++")
        gcc_dir = detect_gcc_install_dir()

    ast_succeeded = False
    if not compiler_bin:
        _add_tool_missing_finding(
            f"Required compiler/AST tool '{'clang' if is_c else 'clang++'}' is not installed or not on PATH"
        )
    else:
        if tool_env is not None and compile_commands is not None and tool_env.compile_commands_entries:
            cc_flags = (
                _extract_flags_from_entries(tool_env.compile_commands_entries, compile_commands.parent, path)
                or infer_repo_include_flags(path)
            )
        else:
            cc_flags = extract_compile_flags(compile_commands, path) or infer_repo_include_flags(path)

        if is_c:
            base_flags = [
                f"-std={cfg.c_std}",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                "-Wswitch-enum",
                "-Wvla",
                "-Wunreachable-code",
                *cc_flags,
            ]
        else:
            exc_flags = ["-fno-exceptions"] if cfg.no_exceptions else []
            rtti_flags = ["-fno-rtti"] if cfg.no_rtti else []
            base_flags = [
                f"-std={cfg.std}",
                *exc_flags,
                *rtti_flags,
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                "-Wswitch-enum",
                "-Woverloaded-virtual",
                "-Wnon-virtual-dtor",
                "-Wsuggest-override",
                "-Wno-error=suggest-override",
                "-Wunreachable-code",
                *cc_flags,
            ]
            if gcc_dir is not None:
                base_flags.append(f"--gcc-install-dir={gcc_dir}")
            if is_header:
                base_flags = ["-x", "c++-header", *base_flags]

        syntax_cmd = [compiler_bin, "-fsyntax-only", *base_flags, str(path)]
        try:
            res = subprocess.run(
                syntax_cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError as exc:
            _add_tool_missing_finding(f"compiler launch failed: {exc}")
            res = None

        if res is not None and res.returncode < 0:
            report.execution_errors.append(f"compiler terminated by signal {-res.returncode}")

        if res is not None:
            recognized = False
            for out_line in res.stdout.splitlines():
                if "[-Wsuggest-override]" in out_line or "[-Winconsistent-missing-override]" in out_line:
                    recognized = True
                    if "MISRA-13.3.1" not in disabled:
                        diag_file, diag_line = _parse_compiler_diagnostic_location(out_line)
                        if not diag_file or pathlib.Path(diag_file).resolve() == path.resolve():
                            override_finding = Finding(
                                str(path),
                                diag_line or 1,
                                "MISRA-13.3.1",
                                "error",
                                "Overriding virtual method must be marked 'override' or 'final'",
                            )
                            report.findings.append(override_finding)
                            if contexts is not None:
                                contexts[id(override_finding)] = FingerprintContext(
                                    tokens=_extract_statement_tokens(raw_text, diag_line or 1),
                                    diagnostic_path=str(path),
                                    extra_roots=extra_roots,
                                )
                    continue
                if ": error:" in out_line or "fatal error:" in out_line or ": warning:" in out_line:
                    recognized = True
                    compile_finding = Finding(str(path), 0, "CLANG-COMPILE", "error", out_line.strip())
                    report.findings.append(compile_finding)
                    if contexts is not None:
                        diag_file, diag_line = _parse_compiler_diagnostic_location(out_line)
                        diag_tokens: tuple[str, ...] = ()
                        if diag_file and diag_line > 0:
                            if diag_file == str(path):
                                diag_tokens = _extract_statement_tokens(raw_text, diag_line)
                            elif pathlib.Path(diag_file).is_file():
                                diag_text = pathlib.Path(diag_file).read_text(encoding="utf-8", errors="replace")
                                diag_tokens = _extract_statement_tokens(diag_text, diag_line)
                        contexts[id(compile_finding)] = FingerprintContext(
                            tokens=diag_tokens,
                            diagnostic_path=diag_file if diag_file else str(path),
                            extra_roots=extra_roots,
                        )
            if res.returncode != 0 and not recognized:
                report.execution_errors.append(
                    f"compiler exited {res.returncode} without a recognized diagnostic"
                )

        ast_cmd = [compiler_bin, "-Xclang", "-ast-dump=json", "-fsyntax-only", *base_flags, str(path)]
        try:
            ast_res = subprocess.run(
                ast_cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            _add_tool_missing_finding(f"AST compiler launch failed: {exc}")
            ast_res = None

        if ast_res is not None and ast_res.returncode < 0:
            report.execution_errors.append(f"AST compiler terminated by signal {-ast_res.returncode}")
        if ast_res is not None and not ast_res.stdout.strip().startswith("{"):
            _add_tool_missing_finding(
                f"AST compiler exited {ast_res.returncode} without producing an AST"
            )
        if ast_res is not None and ast_res.stdout.strip().startswith("{"):
            try:
                ast_root = json.loads(ast_res.stdout)
                ast_Succeeded_flag = isinstance(ast_root, dict) and bool(ast_root)
                ast_succeeded = ast_Succeeded_flag
                ast_findings: list[Finding] = []
                walk_ast_for_misra(
                    ast_root,
                    str(path),
                    raw_text,
                    ast_findings,
                    is_c=is_c,
                    is_header=is_header,
                    max_params=cfg.max_params,
                    config=cfg,
                    plugins=plugins,
                )
                filtered_ast: list[Finding] = []
                existing_rule_lines = {(f.rule, f.line) for f in report.findings}
                for f in ast_findings:
                    if heap_allowed and f.rule in ("MISRA-21.6.1", "MISRA-C-21.3"):
                        continue
                    if f.rule == "MISRA-13.3.1" and (f.rule, f.line) in existing_rule_lines:
                        continue
                    existing_rule_lines.add((f.rule, f.line))
                    filtered_ast.append(f)
                    report.findings.append(f)
                _attach_ast_contexts(
                    ast_root,
                    path,
                    raw_text,
                    filtered_ast,
                    contexts,
                    is_c=is_c,
                    is_header=is_header,
                    max_params=cfg.max_params,
                    config=cfg,
                )
            except json.JSONDecodeError:
                _add_tool_missing_finding("AST compiler produced invalid JSON AST output")

    # 4b. Structural lexical checks & AST fallback (when AST did not succeed)
    existing_rule_lines = {(f.rule, f.line) for f in report.findings}
    _run_structural_lexical_checks(
        stripped,
        str(path),
        is_c=is_c,
        heap_allowed=heap_allowed,
        config=cfg,
        include_ast_fallback=not ast_succeeded,
        existing_rule_lines=existing_rule_lines,
        add_finding_cb=_add_lexical_finding,
    )

    # 5. Optional clang-tidy check (for C++ source files)
    effective_skip_tidy = skip_clang_tidy or cfg.skip_clang_tidy
    clang_tidy = (
        (tool_env.clang_tidy_bin or None)
        if tool_env is not None
        else shutil.which("clang-tidy")
    )
    if not effective_skip_tidy and not is_header and not is_c:
        if not clang_tidy:
            if tool_env is None:
                print(
                    "notice: clang-tidy not found; skipping optional clang-tidy checks",
                    file=sys.stderr,
                )
        else:
            tidy_cmd = [clang_tidy, str(path)]
            if compile_commands is not None and compile_commands.is_file():
                tidy_cmd.extend(["-p", str(compile_commands.parent)])
            cfg_to_use = clang_tidy_cfg or find_repo_clang_tidy(path, repository_root)
            if cfg_to_use is not None:
                tidy_cmd.append(f"--config-file={cfg_to_use}")
            else:
                tidy_cmd.append(f"--config={FALLBACK_CLANG_TIDY_CONFIG}")
            if gcc_dir is not None:
                tidy_cmd.append(f"--extra-arg=--gcc-install-dir={gcc_dir}")
            if compile_commands is None or not compile_commands.is_file():
                repo_flags = infer_repo_include_flags(path)
                exc_flags = ["-fno-exceptions"] if cfg.no_exceptions else []
                rtti_flags = ["-fno-rtti"] if cfg.no_rtti else []
                tidy_cmd.extend(["--", f"-std={cfg.std}", *exc_flags, *rtti_flags, *repo_flags])

            try:
                tidy_res = subprocess.run(
                    tidy_cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                report.execution_errors.append("clang-tidy timed out")
                tidy_res = None
            except OSError as exc:
                report.execution_errors.append(f"clang-tidy launch failed: {exc}")
                tidy_res = None
            if tidy_res is not None and tidy_res.returncode < 0:
                report.execution_errors.append(f"clang-tidy terminated by signal {-tidy_res.returncode}")
            if tidy_res is not None and (
                tidy_res.returncode != 0 or ": warning:" in tidy_res.stdout or ": error:" in tidy_res.stdout
            ):
                recognized = False
                for out_line in tidy_res.stdout.splitlines():
                    if ": error:" in out_line or "fatal error:" in out_line or ": warning:" in out_line:
                        recognized = True
                        if "CLANG-TIDY" in disabled:
                            continue
                        tidy_finding = Finding(str(path), 0, "CLANG-TIDY", "error", out_line.strip())
                        report.findings.append(tidy_finding)
                        if contexts is not None:
                            diag_file, diag_line = _parse_compiler_diagnostic_location(out_line)
                            diag_tokens = ()
                            if diag_file and diag_line > 0:
                                if diag_file == str(path):
                                    diag_tokens = _extract_statement_tokens(raw_text, diag_line)
                                elif pathlib.Path(diag_file).is_file():
                                    diag_text = pathlib.Path(diag_file).read_text(encoding="utf-8", errors="replace")
                                    diag_tokens = _extract_statement_tokens(diag_text, diag_line)
                            contexts[id(tidy_finding)] = FingerprintContext(
                                tokens=diag_tokens,
                                diagnostic_path=diag_file if diag_file else str(path),
                                extra_roots=extra_roots,
                            )
                if tidy_res.returncode != 0 and not recognized:
                    report.execution_errors.append(
                        f"clang-tidy exited {tidy_res.returncode} without a recognized diagnostic"
                    )

    # 6. File-level plugin hooks
    for plugin in plugins:
        for p_finding in plugin.check_file(path, raw_text, stripped, cfg):
            if p_finding.rule not in disabled:
                report.findings.append(p_finding)
                if contexts is not None and id(p_finding) not in contexts:
                    contexts[id(p_finding)] = FingerprintContext(
                        tokens=_extract_statement_tokens(raw_text, p_finding.line),
                        extra_roots=extra_roots,
                    )

    return report


def find_repository_root(hint_paths: Iterable[pathlib.Path]) -> pathlib.Path:
    """
    @brief Locate the repository root from hint paths or current working directory.
    @param[in] hint_paths Candidate paths inside or near the repository.
    @return Resolved repository root path.
    """
    resolved_hints = [path.resolve() for path in hint_paths if path is not None]
    for base in resolved_hints:
        search_chain = [base, *base.parents] if base.is_dir() else [*base.parents]
        for current in search_chain:
            if (
                (current / ".git").exists()
                or (current / "preflight.toml").is_file()
                or (current / "CMakeLists.txt").is_file()
            ):
                return current

    cwd_resolved = pathlib.Path.cwd().resolve()
    if resolved_hints:
        all_under_cwd = True
        for h in resolved_hints:
            try:
                h.relative_to(cwd_resolved)
            except ValueError:
                all_under_cwd = False
                break
        if all_under_cwd:
            return cwd_resolved
        first = resolved_hints[0]
        return first if first.is_dir() else first.parent

    for current in [cwd_resolved, *cwd_resolved.parents]:
        if (
            (current / ".git").exists()
            or (current / "preflight.toml").is_file()
            or (current / "CMakeLists.txt").is_file()
        ):
            return current
    return cwd_resolved


def load_and_validate_manifest(manifest_path: pathlib.Path) -> dict[str, Any]:
    """
    @brief Load and validate a JSON baseline manifest file.
    @param[in] manifest_path Path to the JSON manifest.
    @return Validated manifest dictionary.
    """
    if not manifest_path.is_file():
        raise ValueError(f"baseline manifest does not exist: {manifest_path}")
    try:
        raw_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to parse baseline manifest {manifest_path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise ValueError("baseline manifest root must be a JSON object")
    if raw_data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported baseline schema_version: expected {BASELINE_SCHEMA_VERSION}, "
            f"got {raw_data.get('schema_version')!r}"
        )
    actual_fp_version = raw_data.get("fingerprint_policy_version")
    if actual_fp_version != FINGERPRINT_POLICY_VERSION:
        raise ValueError(
            f"fingerprint_policy_version mismatch in {manifest_path}: "
            f"expected {FINGERPRINT_POLICY_VERSION}, got {actual_fp_version!r}. "
            f"Migration hint: regenerate the baseline manifest by running "
            f"'python3 enforcement/lint/preflight_lint.py --baseline {manifest_path} --baseline-update <targets>'."
        )
    modules = raw_data.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("baseline manifest 'modules' must be an object")

    for mod_name, profiles in modules.items():
        if not isinstance(mod_name, str) or not mod_name or not isinstance(profiles, dict):
            raise ValueError(f"invalid scope record in manifest: {mod_name}")
        for profile_name, record in profiles.items():
            if not isinstance(profile_name, str) or not profile_name or not isinstance(record, dict):
                raise ValueError(f"invalid profile '{profile_name}' for scope '{mod_name}'")
            entries = record.get("entries")
            if not isinstance(entries, list):
                raise ValueError(f"missing 'entries' list for {mod_name}/{profile_name}")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(f"entry must be an object in {mod_name}/{profile_name}")
                for required_key in ("fingerprint", "path", "rule", "severity"):
                    if not isinstance(entry.get(required_key), str) or not entry[required_key]:
                        raise ValueError(f"invalid '{required_key}' in {mod_name}/{profile_name}")
                entry_path = entry["path"]
                if pathlib.PurePosixPath(entry_path).is_absolute() or re.match(r"^[A-Za-z]:[/\\]", entry_path):
                    raise ValueError(
                        f"baseline entry path must be repository-relative (no absolute paths): '{entry_path}'"
                    )
                allowance = entry.get("allowance")
                if isinstance(allowance, bool) or not isinstance(allowance, int) or allowance < 1:
                    raise ValueError(f"invalid allowance '{allowance}' in {mod_name}/{profile_name}")
    return raw_data


def load_legacy_txt_count(baseline_path: pathlib.Path) -> int:
    """
    @brief Parse an integer finding allowance from a legacy .txt baseline file.
    @param[in] baseline_path Path to the legacy .txt baseline file.
    @return Non-negative integer count allowance.
    """
    if not baseline_path.is_file():
        raise ValueError(f"legacy baseline file does not exist: {baseline_path}")
    lines = [
        line.strip()
        for line in baseline_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) != 1:
        raise ValueError(f"legacy baseline file must contain exactly one integer line: {baseline_path}")
    try:
        count = int(lines[0])
    except ValueError as exc:
        raise ValueError(f"invalid integer in legacy baseline file {baseline_path}: {lines[0]}") from exc
    if count < 0:
        raise ValueError(f"legacy baseline count must be non-negative: {count}")
    return count


def _detect_git_revision(repository_root: pathlib.Path) -> str:
    """
    @brief Query HEAD commit SHA for repository_root, or return 'unknown'.
    @param[in] repository_root Repository root directory.
    @return Commit SHA string or 'unknown'.
    """
    try:
        res = run_git(
            ["rev-parse", "HEAD"],
            repo_root=repository_root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, GitSecurityError):
        return "unknown"
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "unknown"


def _collect_tool_metadata(std: str, clang_tidy_cfg: pathlib.Path | None) -> dict[str, str]:
    """
    @brief Collect compiler/linter metadata for baseline manifest provenance.
    @param[in] std C++ standard flag value.
    @param[in] clang_tidy_cfg Explicit clang-tidy config path if provided.
    @return Dictionary of tool metadata strings.
    """
    metadata: dict[str, str] = {"std": std, "linter_version": VERSION}
    if clang_tidy_cfg is not None:
        metadata["clang_tidy_config"] = clang_tidy_cfg.name
    for tool_name in ("clang++", "clang-tidy"):
        tool_bin = shutil.which(tool_name)
        if tool_bin:
            ver_res = subprocess.run(
                [tool_bin, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if ver_res.returncode == 0 and ver_res.stdout.strip():
                metadata[tool_name] = ver_res.stdout.splitlines()[0].strip()
    return metadata


def update_baseline_manifest(
    manifest_path: pathlib.Path,
    scope: str,
    profile: str,
    repository_root: pathlib.Path,
    checked_files: Sequence[pathlib.Path],
    reports: Sequence[FileReport],
    contexts: dict[int, FingerprintContext],
    std: str,
    clang_tidy_cfg: pathlib.Path | None,
) -> None:
    """
    @brief Atomically update one scope/profile record in the JSON baseline manifest.
    @param[in] manifest_path Destination JSON manifest path.
    @param[in] scope Scope name to update (e.g. 'default' or module name).
    @param[in] profile Profile name (e.g. 'release').
    @param[in] repository_root Repository root for path normalization.
    @param[in] checked_files Complete list of files checked in this run.
    @param[in] reports List of FileReport objects produced by the lint run.
    @param[in] contexts Mapping from Finding id to FingerprintContext.
    @param[in] std C++ standard used during the run.
    @param[in] clang_tidy_cfg Optional clang-tidy config path.
    @return None.
    """
    if manifest_path.exists():
        manifest = load_and_validate_manifest(manifest_path)
    else:
        manifest = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "fingerprint_policy_version": FINGERPRINT_POLICY_VERSION,
            "modules": {},
        }

    grouped: dict[str, dict[str, Any]] = {}
    for rep in reports:
        for finding in rep.findings:
            ctx = contexts.get(id(finding))
            fp_digest = finding_fingerprint(finding, repository_root, ctx)
            if fp_digest not in grouped:
                extra_pairs = [(pathlib.Path(item), "<BUILD_ROOT>") for item in ctx.extra_roots] if ctx else []
                rel_path = normalize_diagnostic_path(
                    ctx.diagnostic_path if (ctx and ctx.diagnostic_path) else finding.file,
                    repository_root,
                    extra_pairs,
                )
                if pathlib.PurePosixPath(rel_path).is_absolute():
                    rel_path = pathlib.PurePosixPath(rel_path).name
                norm_msg = normalize_diagnostic_message(
                    finding.message,
                    repository_root,
                    extra_pairs,
                )
                grouped[fp_digest] = {
                    "fingerprint": fp_digest,
                    "path": rel_path,
                    "rule": finding.rule,
                    "severity": finding.severity,
                    "message": norm_msg,
                    "context": {
                        "enclosing_symbol": ctx.enclosing_symbol if ctx else "",
                        "tokens": list(ctx.tokens) if ctx else [],
                        "comment_text": ctx.comment_text if ctx else "",
                    },
                    "allowance": 0,
                    "rationale": "accepted baseline finding",
                }
            grouped[fp_digest]["allowance"] += 1

    sorted_entries = sorted(
        grouped.values(),
        key=lambda item: (item["path"], item["rule"], item["severity"], item["fingerprint"]),
    )
    checked_rel_paths = sorted(
        normalize_diagnostic_path(str(file_path), repository_root)
        for file_path in checked_files
    )

    modules_dict = manifest.setdefault("modules", {})
    module_profiles = modules_dict.setdefault(scope, {})
    module_profiles[profile] = {
        "source_revision": _detect_git_revision(repository_root),
        "tool_metadata": _collect_tool_metadata(std, clang_tidy_cfg),
        "checked_paths": checked_rel_paths,
        "entries": sorted_entries,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(manifest_path.parent),
        prefix=".tmp_baseline_",
        suffix=".json",
        delete=False,
    )
    try:
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_file.name, manifest_path)
    finally:
        if os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)

    print(
        f"BASELINE_UPDATED scope={scope} profile={profile} entries={len(sorted_entries)}",
        file=sys.stderr,
    )


def compare_against_baseline(
    manifest: dict[str, Any],
    scope: str,
    profile: str,
    repository_root: pathlib.Path,
    checked_files: Sequence[pathlib.Path],
    reports: Sequence[FileReport],
    contexts: dict[int, FingerprintContext],
    allow_missing_tools: bool = False,
) -> int:
    """
    @brief Compare observed finding multisets against the selected scope/profile baseline.
    @param[in] manifest Validated JSON baseline manifest.
    @param[in] scope Scope name to compare against.
    @param[in] profile Profile name to compare against.
    @param[in] repository_root Repository root for path normalization.
    @param[in] checked_files Files actually checked in this invocation.
    @param[in] reports List of FileReport objects produced by the lint run.
    @param[in] contexts Mapping from Finding id to FingerprintContext.
    @param[in] allow_missing_tools If True, ignore TOOL-MISSING findings during baseline comparison.
    @return Exit code: 0 if within baseline allowances, 1 if new/excess findings, 2 if missing scope/profile.
    """
    modules_dict = manifest.get("modules", {})
    if scope not in modules_dict or profile not in modules_dict[scope]:
        print(
            f"error: baseline manifest missing entry for scope='{scope}' profile='{profile}'",
            file=sys.stderr,
        )
        return 2

    record = modules_dict[scope][profile]
    baseline_entries = record.get("entries", [])
    baseline_by_fp: dict[str, dict[str, Any]] = {}
    for entry in baseline_entries:
        fp_key = entry["fingerprint"]
        if fp_key in baseline_by_fp:
            baseline_by_fp[fp_key]["allowance"] += entry["allowance"]
        else:
            baseline_by_fp[fp_key] = dict(entry)

    observed_counts: Counter[str] = Counter()
    observed_samples: dict[str, tuple[Finding, str]] = {}
    for rep in reports:
        for finding in rep.findings:
            if allow_missing_tools and finding.rule == "TOOL-MISSING":
                continue
            ctx = contexts.get(id(finding))
            fp_digest = finding_fingerprint(finding, repository_root, ctx)
            observed_counts[fp_digest] += 1
            if fp_digest not in observed_samples:
                extra_pairs = [(pathlib.Path(item), "<BUILD_ROOT>") for item in ctx.extra_roots] if ctx else []
                rel_path = normalize_diagnostic_path(
                    ctx.diagnostic_path if (ctx and ctx.diagnostic_path) else finding.file,
                    repository_root,
                    extra_pairs,
                )
                observed_samples[fp_digest] = (finding, rel_path)

    has_regression = False
    for fp_digest, obs_count in sorted(observed_counts.items()):
        sample_finding, rel_path = observed_samples[fp_digest]
        if fp_digest not in baseline_by_fp:
            has_regression = True
            print(
                f"BASELINE_NEW {rel_path}:{sample_finding.line} [{sample_finding.rule}] "
                f"count={obs_count} fp={fp_digest[:12]}",
                file=sys.stderr,
            )
        else:
            allowance = baseline_by_fp[fp_digest]["allowance"]
            if obs_count > allowance:
                has_regression = True
                print(
                    f"BASELINE_EXCESS {rel_path}:{sample_finding.line} [{sample_finding.rule}] "
                    f"observed={obs_count} allowed={allowance} fp={fp_digest[:12]}",
                    file=sys.stderr,
                )

    checked_rel_paths = {
        normalize_diagnostic_path(str(file_path), repository_root)
        for file_path in checked_files
    }
    for fp_digest, entry in sorted(baseline_by_fp.items()):
        if entry["path"] in checked_rel_paths:
            obs_count = observed_counts.get(fp_digest, 0)
            if obs_count < entry["allowance"]:
                print(
                    f"BASELINE_FIXED {entry['path']} [{entry['rule']}] "
                    f"observed={obs_count} allowed={entry['allowance']} fp={fp_digest[:12]}",
                    file=sys.stderr,
                )

    return 1 if has_regression else 0


@dataclass(frozen=True)
class RuntimeToolEnv:
    """Pre-resolved compiler, linter, and environment metadata for a lint run."""

    clang_bin: str
    clang_version: str
    clangxx_bin: str
    clangxx_version: str
    clang_tidy_bin: str
    clang_tidy_version: str
    gcc_dir: str
    linter_sha256: str
    env_vars: tuple[tuple[str, str], ...]
    compile_commands_path: str
    compile_commands_sha256: str
    compile_commands_entries: tuple[dict[str, Any], ...]


def _sha256_bytes(data: bytes) -> str:
    """
    @brief Compute SHA-256 hex digest of raw bytes.
    @param[in] data Input byte buffer.
    @return Lowercase 64-character hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def _query_tool_version(tool_bin: str) -> str:
    """
    @brief Query --version output for a resolved compiler or linter binary.
    @param[in] tool_bin Resolved path to the tool executable, or empty string.
    @return Stripped --version stdout, or empty string if unavailable.
    """
    if not tool_bin:
        return ""
    try:
        res = subprocess.run(
            [tool_bin, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return ""
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def build_runtime_tool_env(compile_commands: pathlib.Path | None) -> RuntimeToolEnv:
    """
    @brief Resolve compiler/linter binaries, versions, environment variables, and compile DB once.
    @param[in] compile_commands Optional path to compile_commands.json.
    @return Immutable RuntimeToolEnv snapshot for worker threads and cache keys.
    """
    raw_clang = shutil.which("clang")
    clang_bin = str(pathlib.Path(raw_clang).resolve()) if raw_clang else ""
    raw_clangxx = shutil.which("clang++")
    clangxx_bin = str(pathlib.Path(raw_clangxx).resolve()) if raw_clangxx else ""
    raw_tidy = shutil.which("clang-tidy")
    clang_tidy_bin = str(pathlib.Path(raw_tidy).resolve()) if raw_tidy else ""

    gcc_dir = detect_gcc_install_dir() or ""
    linter_sha256 = _sha256_bytes(pathlib.Path(__file__).read_bytes())
    env_vars = tuple((var_name, os.environ.get(var_name, "")) for var_name in INCLUDE_ENV_VARS)

    cc_path_str = ""
    cc_sha256 = ""
    cc_entries: tuple[dict[str, Any], ...] = ()
    if compile_commands is not None and compile_commands.is_file():
        cc_path_str = str(compile_commands.resolve())
        try:
            cc_bytes = compile_commands.read_bytes()
            cc_sha256 = _sha256_bytes(cc_bytes)
            parsed = json.loads(cc_bytes.decode("utf-8", errors="replace"))
            if isinstance(parsed, list):
                cc_entries = tuple(item for item in parsed if isinstance(item, dict))
        except (OSError, json.JSONDecodeError):
            cc_sha256 = ""
            cc_entries = ()

    return RuntimeToolEnv(
        clang_bin=clang_bin,
        clang_version=_query_tool_version(clang_bin),
        clangxx_bin=clangxx_bin,
        clangxx_version=_query_tool_version(clangxx_bin),
        clang_tidy_bin=clang_tidy_bin,
        clang_tidy_version=_query_tool_version(clang_tidy_bin),
        gcc_dir=gcc_dir,
        linter_sha256=linter_sha256,
        env_vars=env_vars,
        compile_commands_path=cc_path_str,
        compile_commands_sha256=cc_sha256,
        compile_commands_entries=cc_entries,
    )


def resolve_jobs(explicit_jobs: int | None) -> int:
    """
    @brief Determine worker count from --jobs or PREFLIGHT_JOBS, validating positive integers.
    @param[in] explicit_jobs Explicit --jobs CLI value, or None if omitted.
    @return Positive integer worker count.
    """
    cpu_count = os.cpu_count() or 1
    if explicit_jobs is not None:
        if explicit_jobs < 1:
            raise ValueError(f"--jobs must be a positive integer, got {explicit_jobs}")
        return explicit_jobs
    raw_env = os.environ.get("PREFLIGHT_JOBS")
    if raw_env is not None:
        try:
            parsed_env = int(raw_env.strip())
        except ValueError as exc:
            raise ValueError(f"PREFLIGHT_JOBS must be a positive integer, got '{raw_env}'") from exc
        if parsed_env < 1:
            raise ValueError(f"PREFLIGHT_JOBS must be a positive integer, got '{raw_env}'")
        return min(parsed_env, cpu_count)
    return min(4, cpu_count)


def resolve_cache_dir(
    explicit_cache_dir: pathlib.Path | None,
    repository_root: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """
    @brief Resolve the lint cache directory from --cache-dir or PREFLIGHT_CACHE_DIR.
    @param[in] explicit_cache_dir Explicit --cache-dir CLI path, or None.
    @param[in] repository_root Optional repository root (unused unless env fallback is set).
    @return Resolved cache directory path, or None if caching is not configured.
    """
    _ = repository_root
    if explicit_cache_dir is not None:
        return explicit_cache_dir
    env_cache_dir = os.environ.get("PREFLIGHT_CACHE_DIR", "").strip()
    if env_cache_dir:
        return pathlib.Path(env_cache_dir)
    return None


def _parse_makefile_deps(dep_output: str) -> list[pathlib.Path] | None:
    """
    @brief Parse compiler -M Makefile dependency output into file paths.
    @param[in] dep_output Raw stdout from clang/clang++ -M.
    @return List of dependency Path objects, or None if the output is malformed.
    """
    joined = re.sub(r"\\\r?\n", " ", dep_output)
    colon_idx = joined.find(":")
    if colon_idx < 0:
        return None
    deps_text = joined[colon_idx + 1:]
    tokens: list[str] = []
    current: list[str] = []
    idx = 0
    text_len = len(deps_text)
    while idx < text_len:
        ch = deps_text[idx]
        if ch == "\\" and idx + 1 < text_len:
            next_ch = deps_text[idx + 1]
            if next_ch in (" ", "\\", "#"):
                current.append(next_ch)
                idx += 2
                continue
        if ch == "$" and idx + 1 < text_len and deps_text[idx + 1] == "$":
            current.append("$")
            idx += 2
            continue
        if ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            idx += 1
            continue
        current.append(ch)
        idx += 1
    if current:
        tokens.append("".join(current))
    return [pathlib.Path(token) for token in tokens]


def _discover_c_cpp_dependencies(
    compiler_bin: str,
    base_flags: Sequence[str],
    target_path: pathlib.Path,
) -> list[tuple[str, str]] | None:
    """
    @brief Discover and hash all transitive headers for target_path via compiler -M.
    @param[in] compiler_bin Resolved clang or clang++ binary path.
    @param[in] base_flags Effective compiler flags used for syntax/AST checks.
    @param[in] target_path Source or header file path being checked.
    @return Sorted list of (resolved_path, sha256) pairs, or None on discovery failure.
    """
    dep_cmd = [compiler_bin, "-M", *base_flags, str(target_path)]
    try:
        res = subprocess.run(
            dep_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    parsed_deps = _parse_makefile_deps(res.stdout)
    if parsed_deps is None:
        return None
    resolved_set: set[pathlib.Path] = set()
    for dep_path in parsed_deps:
        try:
            resolved_dep = dep_path.resolve()
        except OSError:
            return None
        if not resolved_dep.is_file():
            return None
        resolved_set.add(resolved_dep)
    hashed_deps: list[tuple[str, str]] = []
    for resolved_dep in sorted(resolved_set):
        try:
            dep_hash = _sha256_bytes(resolved_dep.read_bytes())
        except OSError:
            return None
        hashed_deps.append((str(resolved_dep), dep_hash))
    return hashed_deps


def _compute_file_cache_key(
    target_path: pathlib.Path,
    config: LintConfig,
    compile_commands: pathlib.Path | None,
    clang_tidy_cfg: pathlib.Path | None,
    tool_env: RuntimeToolEnv,
    repository_root: pathlib.Path | None = None,
) -> str | None:
    """
    @brief Compute a deterministic SHA-256 cache key for a single target file check.
    @param[in] target_path Resolved source or header file path.
    @param[in] config Active LintConfig.
    @param[in] compile_commands Optional compile_commands.json path.
    @param[in] clang_tidy_cfg Optional .clang-tidy config path.
    @param[in] tool_env Pre-resolved compiler/linter/environment snapshot.
    @param[in] repository_root Optional repository root.
    @return 64-character SHA-256 hex digest, or None if dependency discovery fails.
    """
    try:
        source_sha256 = _sha256_bytes(target_path.read_bytes())
    except OSError:
        return None

    resolved_target = str(target_path.resolve())
    is_c = target_path.suffix in C_EXTS
    is_header = target_path.suffix in HEADER_EXTS
    compiler_bin = tool_env.clang_bin if is_c else tool_env.clangxx_bin
    compiler_version = tool_env.clang_version if is_c else tool_env.clangxx_version

    if compile_commands is not None and tool_env.compile_commands_entries:
        cc_flags = (
            _extract_flags_from_entries(tool_env.compile_commands_entries, compile_commands.parent, target_path)
            or infer_repo_include_flags(target_path)
        )
    else:
        cc_flags = extract_compile_flags(compile_commands, target_path) or infer_repo_include_flags(target_path)

    if is_c:
        base_flags = [
            f"-std={config.c_std}",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-Wswitch-enum",
            "-Wvla",
            "-Wunreachable-code",
            *cc_flags,
        ]
    else:
        exc_flags = ["-fno-exceptions"] if config.no_exceptions else []
        rtti_flags = ["-fno-rtti"] if config.no_rtti else []
        base_flags = [
            f"-std={config.std}",
            *exc_flags,
            *rtti_flags,
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-Wswitch-enum",
            "-Woverloaded-virtual",
            "-Wnon-virtual-dtor",
            "-Wsuggest-override",
            "-Wno-error=suggest-override",
            "-Wunreachable-code",
            *cc_flags,
        ]
        if tool_env.gcc_dir:
            base_flags.append(f"--gcc-install-dir={tool_env.gcc_dir}")
        if is_header:
            base_flags = ["-x", "c++-header", *base_flags]

    deps: list[tuple[str, str]] = []
    if compiler_bin:
        discovered = _discover_c_cpp_dependencies(compiler_bin, base_flags, target_path)
        if discovered is None:
            return None
        deps = discovered

    use_tidy = (not config.skip_clang_tidy) and bool(tool_env.clang_tidy_bin) and (not is_header) and (not is_c)
    tidy_cfg_path = ""
    tidy_cfg_sha256 = ""
    if use_tidy:
        cfg_to_use = clang_tidy_cfg or find_repo_clang_tidy(target_path, repository_root)
        if cfg_to_use is not None:
            tidy_cfg_path = str(cfg_to_use.resolve())
            if cfg_to_use.is_file():
                try:
                    tidy_cfg_sha256 = _sha256_bytes(cfg_to_use.read_bytes())
                except OSError:
                    return None
            else:
                tidy_cfg_sha256 = "missing"
        else:
            tidy_cfg_sha256 = _sha256_bytes(FALLBACK_CLANG_TIDY_CONFIG.encode("utf-8"))

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "c_cpp",
        "path": resolved_target,
        "source_sha256": source_sha256,
        "linter_sha256": tool_env.linter_sha256,
        "std": config.std,
        "c_std": config.c_std,
        "allow_new_in_init": config.allow_new_in_init,
        "allowed_heap_paths": list(config.allowed_heap_paths),
        "allow_missing_tools": config.allow_missing_tools,
        "max_params": config.max_params,
        "skip_clang_tidy": config.skip_clang_tidy,
        "disabled_rules": sorted(config.disabled_rules),
        "naming": {
            "class_regex": config.naming.class_regex,
            "function_regex": config.naming.function_regex,
            "variable_regex": config.naming.variable_regex,
            "parameter_regex": config.naming.parameter_regex,
            "member_regex": config.naming.member_regex,
            "private_member_regex": config.naming.private_member_regex,
            "constant_regex": config.naming.constant_regex,
            "namespace_regex": config.naming.namespace_regex,
        },
        "compiler_bin": compiler_bin,
        "compiler_version": compiler_version,
        "clang_tidy_bin": tool_env.clang_tidy_bin if use_tidy else "",
        "clang_tidy_version": tool_env.clang_tidy_version if use_tidy else "",
        "gcc_dir": tool_env.gcc_dir,
        "env_vars": [list(item) for item in tool_env.env_vars],
        "compile_commands_path": tool_env.compile_commands_path,
        "compile_commands_sha256": tool_env.compile_commands_sha256,
        "base_flags": base_flags,
        "clang_tidy_cfg_path": tidy_cfg_path,
        "clang_tidy_cfg_sha256": tidy_cfg_sha256,
        "dependencies": [list(item) for item in deps],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_cached_report(
    cache_dir: pathlib.Path,
    cache_key: str,
) -> tuple[FileReport, dict[int, FingerprintContext]] | None:
    """
    @brief Load and validate a cached FileReport and its FingerprintContexts.
    @param[in] cache_dir Root lint cache directory.
    @param[in] cache_key 64-character cache key hex digest.
    @return Tuple of (FileReport, contexts) on valid hit, or None on miss/corruption.
    """
    entry_path = cache_dir / cache_key[:2] / f"{cache_key}.json"
    if not entry_path.is_file():
        return None
    try:
        data = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != CACHE_SCHEMA_VERSION or data.get("cache_key") != cache_key:
        return None
    rep_obj = data.get("report")
    if not isinstance(rep_obj, dict):
        return None
    rep_path = rep_obj.get("path")
    raw_findings = rep_obj.get("findings")
    if not isinstance(rep_path, str) or not isinstance(raw_findings, list):
        return None

    findings: list[Finding] = []
    local_contexts: dict[int, FingerprintContext] = {}
    for item in raw_findings:
        if not isinstance(item, dict):
            return None
        file_str = item.get("file")
        line_val = item.get("line")
        rule_str = item.get("rule")
        severity_str = item.get("severity")
        message_str = item.get("message")
        if (
            not isinstance(file_str, str)
            or isinstance(line_val, bool)
            or not isinstance(line_val, int)
            or not isinstance(rule_str, str)
            or not isinstance(severity_str, str)
            or not isinstance(message_str, str)
        ):
            return None
        finding = Finding(file_str, line_val, rule_str, severity_str, message_str)
        findings.append(finding)
        raw_ctx = item.get("context")
        if raw_ctx is not None:
            if not isinstance(raw_ctx, dict):
                return None
            tokens_raw = raw_ctx.get("tokens", [])
            extra_raw = raw_ctx.get("extra_roots", [])
            if not isinstance(tokens_raw, list) or not isinstance(extra_raw, list):
                return None
            local_contexts[id(finding)] = FingerprintContext(
                enclosing_symbol=str(raw_ctx.get("enclosing_symbol", "")),
                tokens=tuple(str(tok) for tok in tokens_raw),
                comment_text=str(raw_ctx.get("comment_text", "")),
                diagnostic_path=str(raw_ctx.get("diagnostic_path", "")),
                extra_roots=tuple(str(root) for root in extra_raw),
            )
    return FileReport(rep_path, findings), local_contexts


def _store_cached_report(
    cache_dir: pathlib.Path,
    cache_key: str,
    report: FileReport,
    local_contexts: dict[int, FingerprintContext],
) -> None:
    """
    @brief Atomically write a FileReport and its FingerprintContexts to the cache.
    @param[in] cache_dir Root lint cache directory.
    @param[in] cache_key 64-character cache key hex digest.
    @param[in] report Computed FileReport to store.
    @param[in] local_contexts Mapping from Finding id to FingerprintContext.
    @return None.
    """
    entry_path = cache_dir / cache_key[:2] / f"{cache_key}.json"
    serialized_findings: list[dict[str, Any]] = []
    for finding in report.findings:
        ctx = local_contexts.get(id(finding))
        ctx_payload: dict[str, Any] | None = None
        if ctx is not None:
            ctx_payload = {
                "enclosing_symbol": ctx.enclosing_symbol,
                "tokens": list(ctx.tokens),
                "comment_text": ctx.comment_text,
                "diagnostic_path": ctx.diagnostic_path,
                "extra_roots": list(ctx.extra_roots),
            }
        serialized_findings.append(
            {
                "file": finding.file,
                "line": finding.line,
                "rule": finding.rule,
                "severity": finding.severity,
                "message": finding.message,
                "context": ctx_payload,
            }
        )
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "report": {
            "path": report.path,
            "findings": serialized_findings,
        },
    }
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    tmp_path = ""
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(entry_path.parent),
            prefix=".tmp_cache_",
            suffix=".json",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(serialized)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, entry_path)
    except OSError:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _run_file_checks_with_context(
    path: pathlib.Path,
    config: LintConfig,
    compile_commands: pathlib.Path | None,
    clang_tidy_cfg: pathlib.Path | None,
    tool_env: RuntimeToolEnv,
    cache_dir: pathlib.Path | None,
    no_cache: bool = False,
    repository_root: pathlib.Path | None = None,
    plugins: Sequence[LintPlugin] = (),
) -> tuple[FileReport, dict[int, FingerprintContext]]:
    """
    @brief Execute checks for one file with optional cache lookup and atomic population.
    @param[in] path File path to check.
    @param[in] config Resolved LintConfig.
    @param[in] compile_commands Optional compile_commands.json path.
    @param[in] clang_tidy_cfg Optional .clang-tidy path.
    @param[in] tool_env Pre-resolved compiler/linter/environment snapshot.
    @param[in] cache_dir Resolved cache directory, or None when caching is disabled.
    @param[in] no_cache Whether caching is disabled.
    @param[in] repository_root Optional repository root path.
    @param[in] plugins Loaded plugin instances.
    @return Tuple of (FileReport, local_contexts).
    """
    cache_key: str | None = None
    if not no_cache and cache_dir is not None and not plugins:
        cache_key = _compute_file_cache_key(
            path,
            config,
            compile_commands,
            clang_tidy_cfg,
            tool_env,
            repository_root,
        )
        if cache_key is not None:
            cached = _load_cached_report(cache_dir, cache_key)
            if cached is not None:
                return cached

    local_contexts: dict[int, FingerprintContext] = {}
    if path.suffix in (C_EXTS | CPP_EXTS):
        report = check_c_or_cpp_file(
            path,
            compile_commands,
            clang_tidy_cfg,
            config.std,
            config.allow_new_in_init,
            config.max_params,
            contexts=local_contexts,
            skip_clang_tidy=config.skip_clang_tidy,
            tool_env=tool_env,
            config=config,
            repository_root=repository_root,
            plugins=plugins,
        )
    else:
        report = FileReport(str(path))
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        stripped = strip_comments_and_strings(raw_text)
        for plugin in plugins:
            if path.suffix in plugin.supported_extensions:
                for p_finding in plugin.check_file(path, raw_text, stripped, config):
                    if p_finding.rule not in config.disabled_rules:
                        report.findings.append(p_finding)
                        local_contexts[id(p_finding)] = FingerprintContext(
                            tokens=tokenize_cpp_snippet(raw_text),
                        )

    has_tool_missing = any(f.rule == "TOOL-MISSING" for f in report.findings)
    if (
        not no_cache
        and cache_dir is not None
        and cache_key is not None
        and not report.execution_errors
        and not has_tool_missing
    ):
        _store_cached_report(cache_dir, cache_key, report, local_contexts)
    return report, local_contexts


def build_arg_parser() -> argparse.ArgumentParser:
    """Create and return the CLI ArgumentParser for preflight_lint.py."""
    parser = argparse.ArgumentParser(
        prog="preflight_lint.py",
        description=(
            "Generic C/C++ preflight linter (MISRA-style lexical/AST rules, Doxygen, "
            "configurable naming regexes, parameter limits, optional clang-tidy, and baseline fingerprints). "
            "DOXYGEN-FILE-BLOCK requires @file in Doxygen comments; "
            "DOXYGEN-PARAM-DIR-MISMATCH checks parameter types against directions."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("targets", nargs="*", help="C/C++ files or directories to check")
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=None,
        help="Path to preflight.toml configuration file",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=None,
        help="Repository root directory for path-relative normalization and fingerprints",
    )
    parser.add_argument(
        "--compile-commands",
        type=pathlib.Path,
        default=None,
        help="Path to compile_commands.json",
    )
    parser.add_argument(
        "--clang-tidy-config",
        type=pathlib.Path,
        default=None,
        help="Path to .clang-tidy configuration file",
    )
    parser.add_argument(
        "--std",
        default=None,
        choices=["c++14", "c++17", "c++20"],
        help="C++ standard (default: c++17)",
    )
    parser.add_argument(
        "--c-std",
        default=None,
        choices=["c99", "c11", "c17"],
        help="C standard (default: c11)",
    )
    parser.add_argument(
        "--allow-new-in-init",
        action="store_true",
        help="Allow dynamic memory allocation (new/delete/malloc/free) in *init* files",
    )
    parser.add_argument(
        "--allow-new-paths",
        action="append",
        default=None,
        help="Glob pattern(s) where dynamic heap allocation is allowed (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--allow-missing-tools",
        action="store_true",
        help="Do not fail non-zero on TOOL-MISSING when compiler/AST/compile info is unavailable",
    )
    parser.add_argument(
        "--max-params",
        type=int,
        default=None,
        help=f"Maximum function parameters per HIS-PAR (default: {DEFAULT_MAX_PARAMS})",
    )
    parser.add_argument(
        "--naming-class-regex",
        default=None,
        help="Regex for class/struct/enum/type-alias names",
    )
    parser.add_argument(
        "--naming-function-regex",
        default=None,
        help="Regex for function and method names",
    )
    parser.add_argument(
        "--naming-variable-regex",
        default=None,
        help="Regex for local and global variable names",
    )
    parser.add_argument(
        "--naming-parameter-regex",
        default=None,
        help="Regex for function parameter names",
    )
    parser.add_argument(
        "--naming-member-regex",
        default=None,
        help="Regex for public struct/class member field names",
    )
    parser.add_argument(
        "--naming-private-member-regex",
        default=None,
        help="Regex for private/protected class member field names",
    )
    parser.add_argument(
        "--naming-constant-regex",
        default=None,
        help="Regex for constexpr constant names",
    )
    parser.add_argument(
        "--naming-namespace-regex",
        default=None,
        help="Regex for namespace names",
    )
    parser.add_argument(
        "--disable-rule",
        action="append",
        default=None,
        help="Disable specific rule ID(s) (repeatable)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=None,
        help="Path to a Python plugin file implementing the preflight hook interface (repeatable)",
    )
    parser.add_argument(
        "--allow-config-plugins",
        action="store_true",
        help="Allow loading Python plugin modules declared in [plugins].paths in preflight.toml",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "--baseline",
        type=pathlib.Path,
        default=None,
        help="Path to JSON baseline manifest or legacy .txt count file",
    )
    parser.add_argument(
        "--baseline-scope",
        default=None,
        help=f"Scope name for baseline comparison or update (default: {DEFAULT_BASELINE_SCOPE})",
    )
    parser.add_argument(
        "--baseline-profile",
        default=DEFAULT_BASELINE_PROFILE,
        help=f"Baseline profile name (default: {DEFAULT_BASELINE_PROFILE})",
    )
    parser.add_argument(
        "--baseline-update",
        action="store_true",
        help="Create or update the baseline manifest entry for the checked targets",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Number of parallel worker threads (default: PREFLIGHT_JOBS or min(4, CPU count))",
    )
    parser.add_argument(
        "--cache-dir",
        type=pathlib.Path,
        default=None,
        help="Directory for content-hash lint cache",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable lint cache reads and writes",
    )
    parser.add_argument(
        "--changed-files",
        type=pathlib.Path,
        default=None,
        help="JSON array of repository-relative files whose diagnostic paths are in scope",
    )
    parser.add_argument(
        "--finding-export",
        type=pathlib.Path,
        default=None,
        help="Write normalized diagnostic multiset JSON",
    )
    parser.add_argument(
        "--skip-clang-tidy",
        action="store_true",
        help="Skip clang-tidy checks on C++ files",
    )
    parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated workspace component names to check (overrides [scan] defaults)",
    )
    parser.add_argument(
        "--all-components",
        action="store_true",
        help="Check all configured/discovered workspace components",
    )
    parser.add_argument(
        "--project-config",
        type=pathlib.Path,
        default=None,
        help="Optional path to project.toml (default: <repo-root>/.devpack/project.toml)",
    )
    parser.add_argument(
        "--deviation",
        default="",
        help="Human deviation ID verified against DEVPACK_DEVIATIONS (e.g. for PROCESS-TERMINATION)",
    )
    return parser


def _resolve_config_from_args(
    args: argparse.Namespace,
    repository_root: pathlib.Path,
) -> LintConfig:
    """Build a LintConfig from --config / repository preflight.toml and apply CLI overrides."""
    allow_cfg_plugins = bool(getattr(args, "allow_config_plugins", False))
    if args.config is not None:
        config = load_config_file(args.config, allow_config_plugins=allow_cfg_plugins)
    elif (repository_root / "preflight.toml").is_file():
        config = load_config_file(
            repository_root / "preflight.toml", allow_config_plugins=allow_cfg_plugins
        )
    else:
        config = LintConfig()

    if args.std is not None:
        config.std = args.std
    if args.c_std is not None:
        config.c_std = args.c_std
    if args.max_params is not None:
        if args.max_params < 1:
            raise ValueError(f"--max-params must be a positive integer, got {args.max_params}")
        config.max_params = args.max_params
    if args.allow_missing_tools:
        config.allow_missing_tools = True
    if args.skip_clang_tidy:
        config.skip_clang_tidy = True
    if args.allow_new_in_init:
        config.allow_new_in_init = True
    if args.allow_new_paths:
        extra_paths: list[str] = list(config.allowed_heap_paths)
        for raw_entry in args.allow_new_paths:
            for item in raw_entry.split(","):
                stripped_item = item.strip()
                if stripped_item:
                    extra_paths.append(stripped_item)
        config.allowed_heap_paths = tuple(extra_paths)
    if args.disable_rule:
        for raw_rule in args.disable_rule:
            for item in raw_rule.split(","):
                if item.strip():
                    config.disabled_rules.add(item.strip())
    if args.plugin:
        config.plugin_paths = (*config.plugin_paths, *tuple(args.plugin))

    for cli_attr, naming_attr in (
        ("naming_class_regex", "class_regex"),
        ("naming_function_regex", "function_regex"),
        ("naming_variable_regex", "variable_regex"),
        ("naming_parameter_regex", "parameter_regex"),
        ("naming_member_regex", "member_regex"),
        ("naming_private_member_regex", "private_member_regex"),
        ("naming_constant_regex", "constant_regex"),
        ("naming_namespace_regex", "namespace_regex"),
    ):
        val = getattr(args, cli_attr, None)
        if val is not None:
            setattr(config.naming, naming_attr, val)
    config.naming.validate()
    config.deviation_id = str(getattr(args, "deviation", "") or "").strip()
    return config


def _expand_workspace_targets(
    raw_targets: Sequence[str],
    repository_root: pathlib.Path,
    ws_scope: WorkspaceScope,
) -> list[str]:
    """Expand directory targets against ``ws_scope`` so out-of-workspace trees are never walked."""
    if not ws_scope.configured:
        return list(raw_targets)
    if not raw_targets:
        return [str(d) for d in ws_scope.target_dirs()]

    expanded: list[str] = []
    seen: set[str] = set()
    ws_root_resolved = ws_scope.workspace_root.resolve()
    for raw in raw_targets:
        p = pathlib.Path(raw)
        if not p.is_absolute() and not p.exists():
            repo_cand = repository_root / p
            if repo_cand.exists():
                p = repo_cand
        if not p.exists():
            # Preserve missing target so collect_files reports the exact path error.
            expanded.append(raw)
            continue
        p_res = p.resolve()
        if p_res.is_dir():
            if ws_root_resolved.is_relative_to(p_res):
                for comp_dir in ws_scope.target_dirs():
                    s = str(comp_dir)
                    if s not in seen:
                        seen.add(s)
                        expanded.append(s)
            elif p_res.is_relative_to(ws_root_resolved):
                for comp_dir in ws_scope.target_dirs():
                    comp_res = comp_dir.resolve()
                    if comp_res.is_relative_to(p_res):
                        s = str(comp_res)
                        if s not in seen:
                            seen.add(s)
                            expanded.append(s)
                    elif p_res.is_relative_to(comp_res):
                        s = str(p_res)
                        if s not in seen:
                            seen.add(s)
                            expanded.append(s)
            # Directory outside workspace_root is intentionally not walked.
        else:
            s = str(p_res)
            if s not in seen:
                seen.add(s)
                expanded.append(s)
    return expanded


def main(argv: Iterable[str] | None = None) -> int:
    """
    @brief CLI entry point for generic C/C++ preflight linting and baseline ratcheting.
    @param[in] argv Optional command-line argument sequence; defaults to sys.argv[1:].
    @return Process exit code (0 on pass, 1 on lint/baseline failure, 2 on usage/config error).
    """
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    changed_paths: set[str] | None = None
    if args.changed_files is not None:
        try:
            raw_changed = json.loads(args.changed_files.read_text(encoding="utf-8"))
            if not isinstance(raw_changed, list) or any(not isinstance(p, str) for p in raw_changed):
                raise ValueError("expected a JSON array of strings")
            parsed_paths = [pathlib.PurePosixPath(p) for p in raw_changed]
            if any(path.is_absolute() or ".." in path.parts for path in parsed_paths):
                raise ValueError("paths must be repository-relative and may not escape the repository")
            changed_paths = {path.as_posix() for path in parsed_paths}
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: invalid --changed-files manifest: {exc}", file=sys.stderr)
            return 2

    try:
        jobs = resolve_jobs(args.jobs)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    hint_paths = [pathlib.Path(raw) for raw in args.targets]
    if args.config is not None:
        hint_paths.append(args.config)
    if args.baseline is not None:
        hint_paths.append(args.baseline)

    if args.repo_root is not None:
        repository_root = args.repo_root.resolve()
    elif hint_paths:
        repository_root = find_repository_root(hint_paths)
    else:
        repository_root = pathlib.Path.cwd().resolve()

    if args.project_config is not None and not args.project_config.is_file():
        print(f"error: project config not found: {args.project_config}", file=sys.stderr)
        return 2

    try:
        ws_scope = resolve_workspace_scope(
            repository_root,
            project_config_path=args.project_config,
            components=args.components,
            all_components=bool(args.all_components),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.targets and not ws_scope.configured:
        print("error: provide one or more positional target files or directories", file=sys.stderr)
        return 2

    try:
        config = _resolve_config_from_args(args, repository_root)
        plugins = load_plugins(
            config.plugin_paths,
            args.config.resolve().parent if args.config is not None else repository_root,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    extra_exts = set()
    for plugin in plugins:
        extra_exts.update(plugin.supported_extensions)

    effective_targets = _expand_workspace_targets(args.targets, repository_root, ws_scope)
    files = (
        collect_files(effective_targets, extra_extensions=extra_exts, skip_dirs=config.skip_dirs)
        if effective_targets
        else []
    )
    if ws_scope.configured:
        files = ws_scope.filter(files)
    if config.exclude_globs:
        filtered_files: list[pathlib.Path] = []
        for fp in files:
            rel_p = normalize_diagnostic_path(str(fp), repository_root)
            if any(fnmatch.fnmatch(rel_p, pat) or fnmatch.fnmatch(fp.name, pat) for pat in config.exclude_globs):
                continue
            filtered_files.append(fp)
        files = filtered_files

    if not files:
        if ws_scope.configured:
            if args.json:
                print(json.dumps({"files_checked": 0, "total_findings": 0, "reports": []}, indent=2))
            else:
                print("\nSummary: 0 file(s) checked, 0 finding(s).")
            return 0
        print("preflight_lint: no matching C/C++ files found", file=sys.stderr)
        return 2

    if args.repo_root is None:
        repository_root = find_repository_root([*files, *hint_paths])

    baseline_scope = args.baseline_scope or DEFAULT_BASELINE_SCOPE
    baseline_profile = args.baseline_profile or DEFAULT_BASELINE_PROFILE
    is_txt_baseline = args.baseline is not None and args.baseline.suffix == ".txt"

    if args.baseline_update:
        if args.baseline is None:
            print("error: --baseline-update requires --baseline", file=sys.stderr)
            return 2
        if is_txt_baseline:
            print("error: --baseline-update cannot be used with a legacy .txt baseline", file=sys.stderr)
            return 2

    loaded_manifest: dict[str, Any] | None = None
    if args.baseline is not None and not is_txt_baseline and not args.baseline_update:
        try:
            loaded_manifest = load_and_validate_manifest(args.baseline)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        modules_dict = loaded_manifest.get("modules", {})
        if baseline_scope not in modules_dict or baseline_profile not in modules_dict[baseline_scope]:
            print(
                f"error: baseline manifest missing scope='{baseline_scope}' profile='{baseline_profile}'",
                file=sys.stderr,
            )
            return 2

    tool_env = build_runtime_tool_env(args.compile_commands)

    has_cpp_sources = any(
        fp.suffix in CPP_EXTS and fp.suffix not in HEADER_EXTS for fp in files
    )
    if has_cpp_sources and not config.skip_clang_tidy and not tool_env.clang_tidy_bin:
        print(
            "notice: clang-tidy not found; skipping optional clang-tidy checks",
            file=sys.stderr,
        )

    cache_dir = None if args.no_cache else resolve_cache_dir(args.cache_dir, repository_root)
    supported_suffixes = C_EXTS | CPP_EXTS | extra_exts
    target_files = [fp for fp in files if fp.suffix in supported_suffixes]

    reports: list[FileReport] = []
    contexts: dict[int, FingerprintContext] = {}
    total_findings = 0
    if jobs == 1 or len(target_files) <= 1:
        ordered_results = [
            _run_file_checks_with_context(
                fp,
                config,
                args.compile_commands,
                args.clang_tidy_config,
                tool_env,
                cache_dir,
                no_cache=args.no_cache,
                repository_root=repository_root,
                plugins=plugins,
            )
            for fp in target_files
        ]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = [
                executor.submit(
                    _run_file_checks_with_context,
                    fp,
                    config,
                    args.compile_commands,
                    args.clang_tidy_config,
                    tool_env,
                    cache_dir,
                    args.no_cache,
                    repository_root,
                    plugins,
                )
                for fp in target_files
            ]
            ordered_results = [future.result() for future in futures]

    for rep, local_contexts in ordered_results:
        if changed_paths is not None:
            rep.findings = filter_findings_by_changed_paths(
                rep.findings, local_contexts, repository_root, changed_paths
            )
        reports.append(rep)
        contexts.update(local_contexts)
        total_findings += len(rep.findings)

    execution_errors = [f"{report.path}: {error}" for report in reports for error in report.execution_errors]
    if execution_errors:
        for error in execution_errors:
            print(f"error: lint execution incomplete: {error}", file=sys.stderr)

    if args.json:
        payload = {
            "files_checked": len(reports),
            "total_findings": total_findings,
            "reports": [
                {
                    "path": r.path,
                    "findings": [f.__dict__ for f in r.findings],
                }
                for r in reports
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        for r in reports:
            status = "PASS" if not r.findings else f"FAIL ({len(r.findings)})"
            print(f"[{status}] {r.path}")
            for f in r.findings:
                loc = f"{f.file}:{f.line}" if f.line else f.file
                print(f"  - [{f.severity.upper()}][{f.rule}] {loc}: {f.message}")
        print(f"\nSummary: {len(reports)} file(s) checked, {total_findings} finding(s).")

    if args.finding_export is not None:
        all_findings = [finding for report in reports for finding in report.findings]
        export_rows = normalized_finding_export(all_findings, repository_root, contexts)
        args.finding_export.parent.mkdir(parents=True, exist_ok=True)
        args.finding_export.write_text(json.dumps(export_rows, indent=2) + "\n", encoding="utf-8")

    if execution_errors:
        return 2

    blocking_findings = [
        finding
        for report in reports
        for finding in report.findings
        if not (config.allow_missing_tools and finding.rule == "TOOL-MISSING")
    ]
    has_unallowed_tool_missing = any(
        finding.rule == "TOOL-MISSING" and not config.allow_missing_tools
        for report in reports
        for finding in report.findings
    )

    if args.baseline_update:
        if has_unallowed_tool_missing:
            print(
                "error: refusing to update baseline when TOOL-MISSING findings are present "
                "(pass --allow-missing-tools to override)",
                file=sys.stderr,
            )
            return 1
        try:
            update_baseline_manifest(
                args.baseline,
                baseline_scope,
                baseline_profile,
                repository_root,
                files,
                reports,
                contexts,
                config.std,
                args.clang_tidy_config,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.baseline is not None:
        if has_unallowed_tool_missing:
            return 1
        if is_txt_baseline:
            print(
                f"notice: using legacy .txt count-file baseline fallback: {args.baseline}",
                file=sys.stderr,
            )
            try:
                max_allowed = load_legacy_txt_count(args.baseline)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            return 0 if len(blocking_findings) <= max_allowed else 1

        assert loaded_manifest is not None
        return compare_against_baseline(
            loaded_manifest,
            baseline_scope,
            baseline_profile,
            repository_root,
            files,
            reports,
            contexts,
            allow_missing_tools=config.allow_missing_tools,
        )

    return 0 if len(blocking_findings) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
