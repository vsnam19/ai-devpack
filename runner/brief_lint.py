#!/usr/bin/env python3
# brief_lint.py - Pre-dispatch static linter for worker task briefs.
#
# Usage:
#   python3 runner/brief_lint.py <brief.md> [--repo DIR] [--engine NAME]
#       [--engines-config FILE] [--author orchestrator|human] [--help]
#
# Validates a Markdown worker brief against structural, repository-grounding,
# line-range, symbol, token-budget, scope, and contradiction checks. Prints a
# JSON array of findings [{rule, severity, line, message}, ...].
#
# Exit codes:
#   0 - Clean (no error findings; warnings may be present)
#   1 - One or more error findings detected
#   2 - Invalid command-line arguments or missing input file/directory
"""Deterministic pre-dispatch linter for worker task briefs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

RUNNER_DIR = Path(__file__).resolve().parent
PACK_ROOT = RUNNER_DIR.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from runner.agent_exec import _minimal_toml_loads  # noqa: E402

DEFAULT_CONTEXT_TOKENS: dict[str, int] = {
    "sonnet": 200000,
    "haiku": 200000,
    "local": 32768,
}

# Canonical required headings and accepted template aliases.
REQUIRED_HEADINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Goal", ("goal",)),
    ("Input", ("input", "inputs")),
    ("Expected output", ("expected output", "expected outputs", "expected_output")),
    ("Constraints", ("constraints", "stop conditions", "stop rule", "stop rules", "rules")),
    ("Gate", ("gate", "commands", "commands and expected results", "verification")),
)

SCOPE_HEADING_ALIASES: frozenset[str] = frozenset(
    {"files allowed", "allowed files", "scope", "allowed scope"}
)

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FILE_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_./:-])([A-Za-z0-9_./-]+\.(?:py|c|cpp|cc|cxx|h|hpp|hh|md|toml|json|jsonl|sh|txt|yml|yaml|cmake|patch|html|css|js|ts)):(\d+)(?::\d+)?"
)
_AROUND_LINE_RE = re.compile(r"(?i)\b(?:around|at|near|on)?\s*line\s+(\d+)\b")
_CALL_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NAMED_SYMBOL_RE = re.compile(
    r"(?i)\b(?:function|class|struct|method|symbol)\s+`?([A-Za-z_][A-Za-z0-9_:]*)`?"
)
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./:-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+(?::\d+(?::\d+)?)?)"
)

_PROSE_SLASH_PAIRS: frozenset[str] = frozenset(
    {
        "and/or",
        "before/after",
        "c/c++",
        "cloud/local",
        "error/timeout",
        "files/directories",
        "gate/lint",
        "goal/input",
        "implementer/reviewer",
        "in/out",
        "input/output",
        "low/medium",
        "medium/high",
        "min/max",
        "on/off",
        "pass/fail",
        "read/write",
        "review/fix",
        "source/log",
        "start/end",
        "true/false",
        "warn/error",
        "with/without",
        "yes/no",
    }
)

_KNOWN_FILE_EXTS: frozenset[str] = frozenset(
    {
        ".py",
        ".c",
        ".cpp",
        ".cc",
        ".cxx",
        ".h",
        ".hpp",
        ".hh",
        ".md",
        ".toml",
        ".json",
        ".jsonl",
        ".sh",
        ".txt",
        ".yml",
        ".yaml",
        ".cmake",
        ".patch",
        ".html",
        ".css",
        ".js",
        ".ts",
    }
)

_EXEMPT_OUTPUT_FILES: frozenset[str] = frozenset({"STOP_REPORT.md"})

_IGNORED_SYMBOLS: frozenset[str] = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "alignof",
        "decltype",
        "static_cast",
        "const_cast",
        "reinterpret_cast",
        "dynamic_cast",
        "noexcept",
        "int",
        "void",
        "bool",
        "char",
        "short",
        "long",
        "float",
        "double",
        "unsigned",
        "signed",
        "const",
        "static",
        "struct",
        "class",
        "enum",
        "namespace",
        "template",
        "typename",
        "using",
        "auto",
        "nullptr",
        "true",
        "false",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "size_t",
        "string",
        "vector",
        "array",
        "optional",
        "main",
        "assert",
        "printf",
        "fprintf",
        "print",
        "len",
        "str",
        "int",
        "dict",
        "list",
        "set",
        "tuple",
        "Path",
        "bash",
        "python3",
        "pytest",
        "git",
        "cmake",
        "grep",
        "test",
        "file",
        "line",
        "name",
        "code",
        "type",
    }
)

_SKIP_SCAN_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".worktrees", "runs", "build", ".devpack", "node_modules", "briefs"}
)
_SYMBOL_SCAN_EXTS: frozenset[str] = frozenset(
    {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".py", ".sh", ".js", ".ts", ".rs", ".go", ".java"}
)

_FORBID_VERB_RE = re.compile(
    r"(?i)\b(?:do\s+not|don't|never|must\s+not|forbid(?:den)?|prohibit(?:ed)?)\s+"
    r"(?:modify|edit|touch|change|alter|delete|weaken|remove)\b"
)
_MODIFY_VERB_RE = re.compile(
    r"(?i)\b(?:modify|edit|update|change|alter|delete|remove|rewrite)\b"
)

def estimate_tokens(text: str) -> int:
    """Estimate token count deterministically using a 4-characters-per-token heuristic."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _load_toml_dict(path: Path) -> dict[str, Any]:
    """Load a TOML file as a dictionary using tomllib or the Python 3.10 fallback."""
    raw = path.read_text(encoding="utf-8")
    if tomllib is not None:
        data = tomllib.loads(raw)
    else:  # pragma: no cover
        data = _minimal_toml_loads(raw)
    return data if isinstance(data, dict) else {}


def resolve_engine_brief_budget(
    engine: str,
    engines_config: Path | None = None,
) -> tuple[int, int]:
    """Return ``(brief_token_budget, context_tokens)`` for ``engine``."""
    cfg_path = engines_config
    if cfg_path is None:
        candidate = RUNNER_DIR / "engines.example.toml"
        if candidate.is_file():
            cfg_path = candidate

    ctx_tokens = DEFAULT_CONTEXT_TOKENS.get(engine, 200000)
    brief_max = 0

    if cfg_path is not None and cfg_path.is_file():
        try:
            data = _load_toml_dict(cfg_path)
        except Exception:
            data = {}
        limits = data.get("limits", {})
        if isinstance(limits, Mapping) and isinstance(limits.get("brief_tokens_max"), int):
            brief_max = int(limits["brief_tokens_max"])
        engines = data.get("engines", {})
        if isinstance(engines, Mapping) and engine in engines and isinstance(engines[engine], Mapping):
            spec = engines[engine]
            raw_ctx = spec.get("context_tokens") or spec.get("min_context_tokens")
            if isinstance(raw_ctx, int) and raw_ctx > 0:
                ctx_tokens = int(raw_ctx)
            if isinstance(spec.get("brief_tokens_max"), int) and int(spec["brief_tokens_max"]) > 0:
                brief_max = int(spec["brief_tokens_max"])

    if brief_max > 0:
        return brief_max, ctx_tokens
    return max(1, ctx_tokens // 8), ctx_tokens


def _normalize_heading(raw_title: str) -> str:
    """Normalize a Markdown heading title for canonical section matching."""
    cleaned = re.sub(r"\([^)]*\)", "", raw_title)
    cleaned = cleaned.strip().rstrip(":").strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _parse_sections(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Split brief lines into heading-delimited sections, ignoring fenced code blocks."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "heading": "",
        "normalized": "",
        "level": 0,
        "line": 1,
        "body_lines": [],
    }
    in_fence = False
    for idx, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current["body_lines"].append((idx, line))
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                sections.append(current)
                level = len(m.group(1))
                raw_title = m.group(2).strip()
                current = {
                    "heading": raw_title,
                    "normalized": _normalize_heading(raw_title),
                    "level": level,
                    "line": idx,
                    "body_lines": [],
                }
                continue
        current["body_lines"].append((idx, line))
    sections.append(current)
    return sections


def _match_canonical_section(norm: str) -> str | None:
    """Map a normalized heading string to a canonical required section name, or None."""
    for canonical, aliases in REQUIRED_HEADINGS:
        if norm in aliases:
            return canonical
    return None


def _looks_like_repo_path(token: str, repo_root: Path, top_dirs: frozenset[str]) -> str | None:
    """Return a clean repo-relative path if ``token`` looks like a repo file, else None."""
    clean = token.strip("`'\"(),;[]")
    if not clean or any(ch.isspace() for ch in clean):
        return None
    if "://" in clean or clean.startswith(("/", "~", "$", ".", "-")):
        return None
    if any(ch in clean for ch in "*?{}<>@="):
        return None
    clean = re.sub(r":\d+(?::\d+)?$", "", clean)
    if "/" not in clean:
        return None
    if clean.lower() in _PROSE_SLASH_PAIRS:
        return None
    first_seg = clean.split("/", 1)[0]
    suffix = Path(clean).suffix.lower()
    if suffix in _KNOWN_FILE_EXTS or first_seg in top_dirs:
        return clean
    return None


def _line_path_candidates(line: str) -> list[tuple[str, int]]:
    """Return ``(token, position)`` pairs from backtick spans and bare path-like tokens on one line."""
    candidates: list[tuple[str, int]] = []
    for bm in _BACKTICK_RE.finditer(line):
        for part in bm.group(1).strip().split():
            candidates.append((part, bm.start()))
    for pm in _PATH_TOKEN_RE.finditer(line):
        # A token cut short by a glob metacharacter (e.g. ``src/gen_*.cpp``) is not a path.
        if line[pm.end(1):pm.end(1) + 1] in ("*", "?", "[", "{"):
            continue
        candidates.append((pm.group(1), pm.start()))
    return candidates


def _collect_expected_output_paths(
    sections: Sequence[dict[str, Any]], repo: Path, top_dirs: frozenset[str]
) -> set[str]:
    """Return repo-relative paths mentioned anywhere in the brief's Expected output section."""
    mentioned: set[str] = set()
    for sec in sections:
        if _match_canonical_section(sec["normalized"]) != "Expected output":
            continue
        in_fence = False
        for _, line in sec["body_lines"]:
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for raw_tok, _pos in _line_path_candidates(line):
                rel_path = _looks_like_repo_path(raw_tok, repo, top_dirs)
                if rel_path:
                    mentioned.add(rel_path)
    return mentioned


def _collect_repo_identifiers(repo_root: Path) -> set[str]:
    """Build a set of identifiers found across source and header files in ``repo_root``."""
    idents: set[str] = set()
    if not repo_root.is_dir():
        return idents
    ident_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_SCAN_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() not in _SYMBOL_SCAN_EXTS:
            continue
        if path.stat().st_size > 1_048_576:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in ident_re.finditer(content):
            idents.add(match.group(1))
    return idents


def _extract_scope_entries(sections: Sequence[dict[str, Any]], lines: Sequence[str]) -> tuple[bool, int, list[str]]:
    """Return ``(has_scope_section, line_no, entries)`` for allowed-files declarations."""
    for sec in sections:
        if sec["normalized"] in SCOPE_HEADING_ALIASES:
            entries: list[str] = []
            for _, bline in sec["body_lines"]:
                stripped = bline.strip()
                if not stripped or stripped.lower().startswith("out of scope"):
                    continue
                if stripped.startswith(("-", "*")):
                    item = stripped.lstrip("-* ").strip().strip("`")
                    if item and not (item.startswith("{{") and item.endswith("}}")):
                        entries.append(item)
                elif "`" in stripped:
                    for tok in _BACKTICK_RE.findall(stripped):
                        if tok.strip() and not tok.strip().startswith("{{"):
                            entries.append(tok.strip())
            return True, int(sec["line"]), entries

    inline_re = re.compile(r"(?im)^\s*(?:files\s+allowed|allowed\s+files|allowed\s+globs)\s*:\s*(.+)$")
    for idx, line in enumerate(lines, start=1):
        m = inline_re.match(line)
        if m:
            raw_val = m.group(1).strip()
            entries = [part.strip().strip("`") for part in raw_val.split(",") if part.strip()]
            return True, idx, entries

    return False, 1, []


def _extract_contradiction_targets(text: str) -> set[str]:
    """Extract normalized file paths or test-target keywords from a line or phrase."""
    targets: set[str] = set()
    for span in _BACKTICK_RE.findall(text):
        cleaned = span.strip().rstrip(".,:;")
        if "/" in cleaned or cleaned.endswith(tuple(_KNOWN_FILE_EXTS)):
            targets.add(cleaned.lower())
    for m in _PATH_TOKEN_RE.finditer(text):
        tok = re.sub(r":\d+(?::\d+)?$", "", m.group(1)).lower()
        if tok not in _PROSE_SLASH_PAIRS:
            targets.add(tok)
    if re.search(r"(?i)\bexisting\s+tests?\b|\btests/\*\*\b|\bunit\s+tests?\b", text):
        targets.add("__tests__")
    return targets


def lint_brief_text(
    brief_text: str,
    *,
    repo_root: Path | str = ".",
    engine: str = "",
    engines_config: Path | str | None = None,
    author: str = "orchestrator",
) -> list[dict[str, Any]]:
    """Lint ``brief_text`` against ``repo_root`` and return a list of finding dictionaries.

    Each finding has ``{"rule", "severity", "line", "message"}``. When ``author == "human"``,
    all findings are emitted with ``severity = "warning"``. When ``author == "orchestrator"``,
    structural, path, line-range, budget, scope, and contradiction findings use
    ``severity = "error"`` while ``BRIEF-SYMBOL-MISSING`` remains ``"warning"``.
    """
    if author not in ("orchestrator", "human"):
        raise ValueError(f"Unsupported author mode: {author}")

    repo = Path(repo_root).resolve()
    cfg_path = Path(engines_config).resolve() if engines_config else None
    err_sev = "warning" if author == "human" else "error"

    lines = brief_text.splitlines()
    sections = _parse_sections(lines)
    findings: list[dict[str, Any]] = []

    # 1. BRIEF-HEADINGS
    present_canonicals: set[str] = set()
    for sec in sections:
        canon = _match_canonical_section(sec["normalized"])
        if canon is not None:
            present_canonicals.add(canon)

    for canonical, _ in REQUIRED_HEADINGS:
        if canonical not in present_canonicals:
            findings.append(
                {
                    "rule": "BRIEF-HEADINGS",
                    "severity": err_sev,
                    "line": 1,
                    "message": f"Missing required heading: {canonical}",
                }
            )

    # 2. BRIEF-NO-SCOPE
    has_scope, scope_line, scope_entries = _extract_scope_entries(sections, lines)
    if not has_scope or not scope_entries:
        findings.append(
            {
                "rule": "BRIEF-NO-SCOPE",
                "severity": err_sev,
                "line": scope_line,
                "message": "Brief is missing a non-empty allowed-files list (e.g. '## Files allowed').",
            }
        )

    # 3. BRIEF-TOO-LONG
    est_tokens = estimate_tokens(brief_text)
    budget_tokens, _ = resolve_engine_brief_budget(engine, cfg_path)
    if est_tokens > budget_tokens:
        eng_label = engine or "default"
        findings.append(
            {
                "rule": "BRIEF-TOO-LONG",
                "severity": err_sev,
                "line": 1,
                "message": (
                    f"Estimated brief size ({est_tokens} tokens) exceeds {eng_label} "
                    f"budget ({budget_tokens} tokens)."
                ),
            }
        )

    # Pre-collect top-level repo directories and line counts lazily.
    top_dirs = frozenset(
        p.name for p in repo.iterdir() if p.is_dir() and p.name not in _SKIP_SCAN_DIRS
    ) if repo.is_dir() else frozenset()
    line_count_cache: dict[Path, int] = {}

    def _file_line_count(path: Path) -> int:
        if path not in line_count_cache:
            try:
                line_count_cache[path] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                line_count_cache[path] = 0
        return line_count_cache[path]

    # 4 & 5. BRIEF-PATH-MISSING and BRIEF-LINE-OUT-OF-RANGE
    seen_missing_paths: set[tuple[int, str]] = set()
    seen_oob: set[tuple[int, str, int]] = set()
    last_existing_file: Path | None = None
    expected_paths = _collect_expected_output_paths(sections, repo, top_dirs)

    for sec in sections:
        sec_canon = _match_canonical_section(sec["normalized"])
        in_fence = False
        for lineno, line in sec["body_lines"]:
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            # Explicit file:line references
            for m in _FILE_LINE_RE.finditer(line):
                rel = m.group(1)
                target_line = int(m.group(2))
                candidate = repo / rel
                if candidate.is_file():
                    last_existing_file = candidate
                    total = _file_line_count(candidate)
                    if target_line < 1 or target_line > total:
                        key = (lineno, rel, target_line)
                        if key not in seen_oob:
                            seen_oob.add(key)
                            findings.append(
                                {
                                    "rule": "BRIEF-LINE-OUT-OF-RANGE",
                                    "severity": err_sev,
                                    "line": lineno,
                                    "message": f"Line reference '{rel}:{target_line}' exceeds file length ({total} lines).",
                                }
                            )

            # Collect candidate paths from backticks and plain path tokens
            line_files: list[Path] = []
            for raw_tok, _pos in _line_path_candidates(line):
                rel_path = _looks_like_repo_path(raw_tok, repo, top_dirs)
                if not rel_path:
                    continue
                if Path(rel_path).name in _EXEMPT_OUTPUT_FILES or rel_path in _EXEMPT_OUTPUT_FILES:
                    continue
                resolved = repo / rel_path
                if resolved.exists():
                    if resolved.is_file():
                        line_files.append(resolved)
                        last_existing_file = resolved
                    continue
                # Section rule: a missing path blocks only when Goal or Input names it and Expected
                # output does not. Mentions elsewhere (Expected output, Files allowed, Gate, ...)
                # are warnings, because they may describe files the work is meant to create.
                blocking = sec_canon in ("Goal", "Input") and rel_path not in expected_paths
                if (lineno, rel_path) not in seen_missing_paths:
                    seen_missing_paths.add((lineno, rel_path))
                    findings.append(
                        {
                            "rule": "BRIEF-PATH-MISSING",
                            "severity": err_sev if blocking else "warning",
                            "line": lineno,
                            "message": f"Referenced path '{rel_path}' does not exist in repository.",
                        }
                    )

            # "around line N" / "line N" references
            target_for_around = line_files[0] if len(line_files) == 1 else last_existing_file
            if target_for_around is not None and target_for_around.is_file():
                for lm in _AROUND_LINE_RE.finditer(line):
                    ref_n = int(lm.group(1))
                    total = _file_line_count(target_for_around)
                    rel_display = target_for_around.relative_to(repo).as_posix()
                    if ref_n < 1 or ref_n > total:
                        key = (lineno, rel_display, ref_n)
                        if key not in seen_oob:
                            seen_oob.add(key)
                            findings.append(
                                {
                                    "rule": "BRIEF-LINE-OUT-OF-RANGE",
                                    "severity": err_sev,
                                    "line": lineno,
                                    "message": f"Line reference 'line {ref_n}' for '{rel_display}' exceeds file length ({total} lines).",
                                }
                            )

    # 6. BRIEF-SYMBOL-MISSING (warning only)
    repo_idents: set[str] | None = None
    seen_missing_syms: set[tuple[int, str]] = set()
    for sec in sections:
        in_fence = False
        for lineno, line in sec["body_lines"]:
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            sym_candidates: list[str] = []
            for bm in _BACKTICK_RE.finditer(line):
                span = bm.group(1).strip()
                # Look for call-like patterns e.g. `fooBar(` or `ns::Class::foo()`
                for cm in _CALL_IDENT_RE.finditer(span):
                    sym_candidates.append(cm.group(1))
            for nm in _NAMED_SYMBOL_RE.finditer(line):
                raw_sym = nm.group(1).split("::")[-1]
                if raw_sym:
                    sym_candidates.append(raw_sym)

            for sym in sym_candidates:
                if len(sym) < 3 or sym in _IGNORED_SYMBOLS:
                    continue
                if repo_idents is None:
                    repo_idents = _collect_repo_identifiers(repo)
                if sym not in repo_idents and (lineno, sym) not in seen_missing_syms:
                    seen_missing_syms.add((lineno, sym))
                    findings.append(
                        {
                            "rule": "BRIEF-SYMBOL-MISSING",
                            "severity": "warning",
                            "line": lineno,
                            "message": f"Referenced symbol '{sym}' was not found in repository.",
                        }
                    )

    # 7. BRIEF-CONTRADICTION (between Constraints/Scope and Expected output)
    forbidden_targets: list[tuple[int, str, str]] = []
    expected_mods: list[tuple[int, str, str]] = []
    for sec in sections:
        canon = _match_canonical_section(sec["normalized"])
        is_constraint = canon == "Constraints" or sec["normalized"] in SCOPE_HEADING_ALIASES
        is_expected = canon == "Expected output"
        if not (is_constraint or is_expected):
            continue
        for lineno, line in sec["body_lines"]:
            if is_constraint and (_FORBID_VERB_RE.search(line) or "out of scope" in line.lower()):
                for tgt in _extract_contradiction_targets(line):
                    forbidden_targets.append((lineno, tgt, line.strip()))
            if is_expected and _MODIFY_VERB_RE.search(line) and not _FORBID_VERB_RE.search(line):
                for tgt in _extract_contradiction_targets(line):
                    expected_mods.append((lineno, tgt, line.strip()))

    reported_contradictions: set[tuple[int, str]] = set()
    for c_line, c_tgt, _ in forbidden_targets:
        for e_line, e_tgt, _ in expected_mods:
            if c_tgt == e_tgt and (e_line, e_tgt) not in reported_contradictions:
                reported_contradictions.add((e_line, e_tgt))
                findings.append(
                    {
                        "rule": "BRIEF-CONTRADICTION",
                        "severity": err_sev,
                        "line": e_line,
                        "message": (
                            f"Expected output on line {e_line} modifies '{e_tgt}', "
                            f"which is forbidden by Constraints on line {c_line}."
                        ),
                    }
                )

    findings.sort(key=lambda item: (int(item["line"]), str(item["rule"]), str(item["message"])))
    return findings


def lint_brief(
    brief_path: Path | str,
    *,
    repo_root: Path | str = ".",
    engine: str = "",
    engines_config: Path | str | None = None,
    author: str = "orchestrator",
) -> list[dict[str, Any]]:
    """Read ``brief_path`` and run ``lint_brief_text``."""
    path = Path(brief_path)
    text = path.read_text(encoding="utf-8")
    return lint_brief_text(
        text,
        repo_root=repo_root,
        engine=engine,
        engines_config=engines_config,
        author=author,
    )


def has_errors(findings: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if any finding in ``findings`` has ``severity == 'error'``."""
    return any(str(item.get("severity", "")).lower() == "error" for item in findings)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/brief_lint.py``."""
    parser = argparse.ArgumentParser(
        prog="brief_lint.py",
        description="Lint a worker brief before dispatch and emit JSON findings.",
    )
    parser.add_argument("brief", help="Path to the Markdown brief file.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository root directory for path, line-range, and symbol checks (default: .).",
    )
    parser.add_argument(
        "--engine",
        default="",
        help="Target engine name for brief size budget lookup (e.g. haiku, sonnet, local).",
    )
    parser.add_argument(
        "--engines-config",
        default="",
        help="Optional path to engines.toml (default: runner/engines.example.toml).",
    )
    parser.add_argument(
        "--author",
        choices=("orchestrator", "human"),
        default="orchestrator",
        help="Brief author role: orchestrator emits errors; human downgrades findings to warnings.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/brief_lint.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    brief_path = Path(args.brief)
    repo_root = Path(args.repo)
    if not brief_path.is_file():
        print(f"error: brief file not found: {brief_path}", file=sys.stderr)
        return 2
    if not repo_root.is_dir():
        print(f"error: repository directory not found: {repo_root}", file=sys.stderr)
        return 2
    engines_cfg = Path(args.engines_config) if args.engines_config else None
    if engines_cfg is not None and not engines_cfg.is_file():
        print(f"error: engines config not found: {engines_cfg}", file=sys.stderr)
        return 2

    findings = lint_brief(
        brief_path,
        repo_root=repo_root,
        engine=args.engine,
        engines_config=engines_cfg,
        author=args.author,
    )
    print(json.dumps(findings, indent=2))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
