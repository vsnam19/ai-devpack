#!/usr/bin/env python3
# repomap.py - Deterministic stdlib-only C/C++ repository code index generator.
#
# Usage:
#   python3 runner/repomap.py --repo DIR [--task FILE] [--budget N] [--ctags] [--help]
#
# Builds a ranked, token-budgeted Markdown index of a C/C++ repository containing:
#   - The repository file tree
#   - Public classes, structs, enums, and function/method signatures with Doxygen @brief
#
# Exit codes:
#   0 - Code map generated successfully
#   1 - Runtime error reading repository
#   2 - Invalid command-line arguments or missing repository/task file
"""Deterministic stdlib-only C/C++ code map and symbol index generator."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable, Sequence

DEFAULT_BUDGET_TOKENS = 1500

_CPP_EXTS: frozenset[str] = frozenset({".h", ".hpp", ".hh", ".hxx", ".c", ".cpp", ".cc", ".cxx"})
_HEADER_EXTS: frozenset[str] = frozenset({".h", ".hpp", ".hh", ".hxx"})
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".worktrees",
        "runs",
        "build",
        ".devpack",
        "node_modules",
        ".claude",
        "briefs",
    }
)

_REQ_ID_RE = re.compile(r"\b(REQ-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b")
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_CAMEL_SPLIT_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|\d+")
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "when", "where",
        "which", "while", "must", "shall", "should", "not", "only", "when", "after",
        "before", "until", "instead", "against", "file", "files", "allowed", "task",
        "goal", "category", "tier", "commands", "stop", "rule", "true", "false",
        "const", "void", "bool", "int", "char", "double", "float", "auto", "return",
        "class", "struct", "enum", "public", "private", "protected", "namespace",
        "using", "noexcept", "override", "final", "virtual", "static", "inline",
        "explicit", "nullptr", "std", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t", "size_t", "cstdint", "cstddef",
    }
)


@dataclass(frozen=True)
class SymbolEntry:
    """Single indexed C/C++ symbol with kind, normalized signature, and Doxygen brief."""

    kind: str
    name: str
    signature: str
    brief: str = ""
    line: int = 1


@dataclass
class FileIndex:
    """Extracted file metadata, Doxygen file brief, public symbols, and rank score."""

    rel_path: str
    file_brief: str = ""
    symbols: list[SymbolEntry] = field(default_factory=list)
    raw_text: str = ""
    score: int = 0


def estimate_tokens(text: str) -> int:
    """Estimate token count deterministically using 4 characters per token (ceiling)."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _normalize_ws(text: str) -> str:
    """Collapse whitespace runs into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_doxygen_comment(raw_comment: str) -> tuple[bool, str]:
    """Extract ``(is_file_doc, brief_text)`` from a ``/** ... */`` or ``///`` comment."""
    lines: list[str] = []
    for raw in raw_comment.splitlines():
        s = raw.strip()
        if s.startswith("/**"):
            s = s[3:]
        elif s.startswith("/*"):
            s = s[2:]
        if s.endswith("*/"):
            s = s[:-2]
        s = s.lstrip("*/!").strip()
        if s:
            lines.append(s)

    joined = " ".join(lines)
    is_file = bool(re.search(r"(?:@|\\)file\b", joined))

    # Extract @brief / \brief up to next @tag / \tag
    m = re.search(r"(?:@|\\)brief\s+(.+?)(?=(?:@|\\)(?:param|tparam|return|retval|note|warning|req|details|file|see|brief)\b|$)", joined)
    if m:
        return is_file, _normalize_ws(m.group(1))
    # Fallback for plain doc comment without @brief tag
    cleaned = re.sub(r"(?:@|\\)[A-Za-z_]+\b[^@\\]*", "", joined).strip()
    return is_file, _normalize_ws(cleaned)


def _strip_comments_preserve_newlines(text: str) -> tuple[str, list[tuple[int, bool, str]]]:
    """Strip C/C++ comments and literals for structural scanning while collecting Doxygen blocks.

    Returns ``(code_only_text, doc_comments)`` where ``doc_comments`` is a list of
    ``(end_line, is_file_doc, brief_text)``. Preserves line numbers in ``code_only_text``.
    """
    out: list[str] = []
    docs: list[tuple[int, bool, str]] = []
    n = len(text)
    i = 0
    line = 1

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # Block comment
        if ch == "/" and nxt == "*":
            start = i
            is_doc = (i + 2 < n and text[i + 2] in ("*", "!"))
            i += 2
            while i < n:
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    i += 2
                    break
                if text[i] == "\n":
                    line += 1
                    out.append("\n")
                else:
                    out.append(" ")
                i += 1
            # Pad the comment delimiters with spaces
            raw_block = text[start:i]
            newlines_in_block = raw_block.count("\n")
            # Keep out length aligned by replacing only non-newlines; already appended per char except delimiters
            out.extend(" " for _ in range(min(4, len(raw_block) - newlines_in_block)))
            if is_doc:
                is_file, brief = _parse_doxygen_comment(raw_block)
                if brief:
                    docs.append((line, is_file, brief))
            continue

        # Line comment (possibly consecutive /// or //! lines)
        if ch == "/" and nxt == "/":
            is_doc = (i + 2 < n and text[i + 2] in ("/", "!"))
            if is_doc:
                doc_lines: list[str] = []
                while i < n and text[i : i + 2] == "//" and (i + 2 < n and text[i + 2] in ("/", "!")):
                    end_line_pos = text.find("\n", i)
                    if end_line_pos == -1:
                        doc_lines.append(text[i:])
                        i = n
                    else:
                        doc_lines.append(text[i:end_line_pos])
                        out.append("\n")
                        line += 1
                        i = end_line_pos + 1
                is_file, brief = _parse_doxygen_comment("\n".join(doc_lines))
                if brief:
                    docs.append((line - 1 if doc_lines else line, is_file, brief))
            else:
                while i < n and text[i] != "\n":
                    i += 1
            continue

        # String or char literal
        if ch in ('"', "'"):
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                c2 = text[i]
                if c2 == "\\":
                    if i + 1 < n and text[i + 1] == "\n":
                        out.append("\n")
                        line += 1
                    i += 2
                    continue
                if c2 == "\n":
                    out.append("\n")
                    line += 1
                    i += 1
                    break
                if c2 == quote:
                    out.append(" ")
                    i += 1
                    break
                i += 1
            continue

        if ch == "\n":
            line += 1
        out.append(ch)
        i += 1

    return "".join(out), docs


def _match_doc_for_line(
    docs: Sequence[tuple[int, bool, str]],
    start_line: int,
    used_indices: set[int],
) -> str:
    """Return the closest preceding non-file Doxygen @brief within 3 lines of ``start_line``."""
    best_idx = -1
    best_dist = 4
    for idx, (doc_end_line, is_file, brief) in enumerate(docs):
        if idx in used_indices or is_file or not brief:
            continue
        dist = start_line - doc_end_line
        if 0 <= dist <= 3 and dist < best_dist:
            best_dist = dist
            best_idx = idx
    if best_idx >= 0:
        used_indices.add(best_idx)
        return docs[best_idx][2]
    return ""


def _split_top_statements(code_text: str) -> list[tuple[int, str, str]]:
    """Split C/C++ code into structural items while tracking braces, scopes, and access.

    Yields ``(start_line, kind, header_or_stmt)`` for public declarations/definitions,
    skipping function bodies, enum enumerator bodies, private/protected class regions,
    and anonymous namespaces.
    """
    results: list[tuple[int, str, str]] = []
    lines = code_text.splitlines()
    # Scope stack entries: (kind, access, is_anonymous_ns, skip_body)
    # kind in {"namespace", "class", "struct", "enum", "func", "other"}
    scope_stack: list[dict[str, object]] = []
    buf: list[str] = []
    stmt_start_line: int | None = None
    paren_depth = 0
    in_preprocessor_cont = False

    def _in_skip_scope() -> bool:
        """Return True if inside a function/enum body, anonymous namespace, or non-public class section."""
        for sc in scope_stack:
            if sc["skip_body"]:
                return True
            if sc["is_anon"]:
                return True
        if scope_stack:
            top = scope_stack[-1]
            if top["kind"] in ("class", "struct") and top["access"] != "public":
                return True
        return False

    def _clean_stmt(raw: str) -> str:
        """Normalize statement text."""
        return _normalize_ws(raw)

    def _classify_and_record(stmt: str, sline: int, term: str) -> str:
        """Record class/struct/enum/function declarations and return the opened brace scope kind."""
        if not stmt:
            return "other"
        if _in_skip_scope():
            return "other"

        # Namespace
        ns_m = re.match(r"^namespace\s*([A-Za-z_][A-Za-z0-9_:]*)?$", stmt)
        if ns_m and term == "{":
            return "anon_namespace" if not ns_m.group(1) else "namespace"

        # Enum
        enum_m = re.match(r"^enum(?:\s+class|\s+struct)?\s+([A-Za-z_][A-Za-z0-9_]*)\b", stmt)
        if enum_m:
            if term == "{":
                results.append((sline, "enum", stmt))
                return "enum"
            return "other"

        # Class / struct
        cls_m = re.match(
            r"^(?:template\s*<[^>]*>\s*)?(class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            stmt,
        )
        if cls_m and term == "{":
            kind = cls_m.group(1)
            results.append((sline, kind, stmt))
            return kind

        # Skip non-function declarations (using, typedef, friend, constexpr vars, member fields)
        if re.match(r"^(?:using|typedef|friend|static_assert)\b", stmt):
            return "other"

        # Constructor / destructor / function / method (has parentheses)
        # Strip constructor member initializer list ": m_a(1), m_b(2)" before trailing check
        sig_head = stmt
        if term == "{":
            # Look for ") ... :" outside parens for constructor initializer lists
            depth = 0
            close_idx = -1
            for idx_c, c in enumerate(stmt):
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        close_idx = idx_c
                        break
            if close_idx != -1:
                tail = stmt[close_idx + 1 :]
                colon_m = re.search(r"\s*:\s*[A-Za-z_]", tail)
                if colon_m:
                    sig_head = stmt[: close_idx + 1 + colon_m.start()].strip()

        if "(" in sig_head and ")" in sig_head:
            # Avoid variable initializations like `const Foo x(1);` vs function prototypes
            # Require either `{` (definition), or `noexcept`/`const`/`override`/`final`/`= 0` or return type / ctor
            if not re.match(r"^(?:return|if|for|while|switch|catch|sizeof|decltype)\b", sig_head):
                # Extract function name immediately before '('
                paren_pos = sig_head.find("(")
                prefix = sig_head[:paren_pos].strip()
                # Ignore macro invocations or variable declarations with `=` before `(`
                if "=" not in prefix and prefix:
                    name_m = re.search(r"([~A-Za-z_][A-Za-z0-9_:~]*)$", prefix)
                    if name_m:
                        fn_name = name_m.group(1)
                        # Distinguish `Type var(arg)` from `Type fn(Type arg)` / `explicit Ctor(Type arg)`
                        is_ctor_or_dtor = (
                            fn_name.startswith("~")
                            or "::" in fn_name
                            or prefix.startswith("explicit ")
                            or (
                                scope_stack
                                and scope_stack[-1]["kind"] in ("class", "struct")
                                and fn_name == scope_stack[-1].get("name")
                            )
                        )
                        has_return_or_qual = (" " in prefix) or is_ctor_or_dtor
                        if has_return_or_qual:
                            results.append((sline, "function", sig_head))
                            return "func" if term == "{" else "other"

        return "func" if term == "{" else "other"

    for lineno, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if in_preprocessor_cont:
            in_preprocessor_cont = stripped.endswith("\\")
            continue
        if stripped.startswith("#") and paren_depth == 0 and not "".join(buf).strip():
            buf.clear()
            stmt_start_line = None
            in_preprocessor_cont = stripped.endswith("\\")
            continue

        i = 0
        while i < len(raw_line):
            ch = raw_line[i]

            if ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1

            # Check access specifier label inside class/struct when buffer has `public:` etc.
            if ch == ":" and paren_depth == 0 and not _in_skip_scope_only_body(scope_stack):
                candidate = _clean_stmt("".join(buf))
                if candidate in ("public", "protected", "private"):
                    if scope_stack and scope_stack[-1]["kind"] in ("class", "struct"):
                        scope_stack[-1]["access"] = candidate
                    buf.clear()
                    stmt_start_line = None
                    i += 1
                    continue

            if ch in ("{", "}", ";") and paren_depth == 0:
                stmt_txt = _clean_stmt("".join(buf))
                sline = stmt_start_line or lineno
                buf.clear()
                stmt_start_line = None

                if ch == "{":
                    kind = _classify_and_record(stmt_txt, sline, "{")
                    cls_name = ""
                    if kind in ("class", "struct"):
                        m_name = re.search(r"(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)", stmt_txt)
                        if m_name:
                            cls_name = m_name.group(1)
                    scope_stack.append(
                        {
                            "kind": "namespace" if kind == "anon_namespace" else kind,
                            "name": cls_name,
                            "access": "public" if kind == "struct" else "private",
                            "is_anon": kind == "anon_namespace",
                            "skip_body": kind in ("enum", "func", "other"),
                        }
                    )
                elif ch == "}":
                    if scope_stack:
                        scope_stack.pop()
                else:
                    _classify_and_record(stmt_txt, sline, ";")
                i += 1
                continue

            if not ch.isspace() and stmt_start_line is None:
                stmt_start_line = lineno
            buf.append(ch)
            i += 1
        if buf:
            buf.append("\n")

    return results


def _in_skip_scope_only_body(scope_stack: Sequence[dict[str, object]]) -> bool:
    """Return True if currently inside a function body, enum body, or anonymous namespace."""
    for sc in scope_stack:
        if sc["skip_body"] or sc["is_anon"]:
            return True
    return False


def _extract_symbol_name(kind: str, stmt: str) -> str:
    """Extract the primary identifier name from a C/C++ class, struct, enum, or function signature."""
    if kind in ("class", "struct"):
        m = re.search(r"\b(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)", stmt)
        return m.group(1) if m else stmt
    if kind == "enum":
        m = re.search(r"\benum(?:\s+class|\s+struct)?\s+([A-Za-z_][A-Za-z0-9_]*)", stmt)
        return m.group(1) if m else stmt
    paren = stmt.find("(")
    prefix = stmt[:paren].strip() if paren != -1 else stmt
    m = re.search(r"([~A-Za-z_][A-Za-z0-9_:~]*)$", prefix)
    return m.group(1) if m else prefix


def extract_cpp_file_index(path: Path, rel_path: str) -> FileIndex:
    """Parse one C/C++ header or source file into a ``FileIndex``."""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return FileIndex(rel_path=rel_path)

    code_only, docs = _strip_comments_preserve_newlines(raw_text)
    file_brief = ""
    for _line, is_file, brief in docs:
        if is_file and brief:
            file_brief = brief
            break

    used_docs: set[int] = set()
    symbols: list[SymbolEntry] = []
    seen_sigs: set[tuple[str, str]] = set()
    for sline, kind, stmt in _split_top_statements(code_only):
        name = _extract_symbol_name(kind, stmt)
        brief = _match_doc_for_line(docs, sline, used_docs)
        key = (kind, stmt)
        if key in seen_sigs:
            continue
        seen_sigs.add(key)
        symbols.append(
            SymbolEntry(
                kind=kind,
                name=name,
                signature=stmt,
                brief=brief,
                line=sline,
            )
        )

    return FileIndex(
        rel_path=rel_path,
        file_brief=file_brief,
        symbols=symbols,
        raw_text=raw_text,
    )


def _collect_ctags_symbols(repo_root: Path, cpp_files: Sequence[str]) -> dict[str, list[SymbolEntry]]:
    """Optionally run ``ctags`` if available on PATH and parse tag entries per file."""
    ctags_bin = shutil.which("ctags")
    if not ctags_bin or not cpp_files:
        return {}
    try:
        proc = subprocess.run(
            [ctags_bin, "--output-format=json", "--fields=+nKz", "-f", "-", *cpp_files],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    by_file: dict[str, list[SymbolEntry]] = {}
    for raw_line in proc.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = __import__("json").loads(raw_line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        fpath = str(obj.get("path") or "")
        name = str(obj.get("name") or "")
        kind = str(obj.get("kind") or "")
        if not fpath or not name or kind in ("member", "local", "variable"):
            continue
        access = str(obj.get("access") or "")
        if access in ("private", "protected"):
            continue
        pattern = str(obj.get("pattern") or "").strip("/^$ ")
        sig = _normalize_ws(pattern.rstrip("{; ")) or name
        mapped_kind = "function" if kind in ("function", "prototype", "method") else kind
        by_file.setdefault(fpath, []).append(
            SymbolEntry(
                kind=mapped_kind,
                name=name,
                signature=sig,
                line=int(obj.get("line", 1) or 1),
            )
        )
    return by_file


def _split_subtokens(ident: str) -> set[str]:
    """Split a snake_case or camelCase identifier into lowercased subtokens of length >= 3."""
    tokens: set[str] = set()
    for part in ident.split("_"):
        if len(part) >= 3 and part.lower() not in _STOP_WORDS:
            tokens.add(part.lower())
        for sub in _CAMEL_SPLIT_RE.findall(part):
            low = sub.lower()
            if len(low) >= 3 and low not in _STOP_WORDS:
                tokens.add(low)
    return tokens


def _extract_spec_req_map(repo_root: Path) -> dict[str, tuple[set[str], set[str]]]:
    """Index requirement sections from ``docs/spec.md`` or ``requirements.txt`` by REQ-ID."""
    req_map: dict[str, tuple[set[str], set[str]]] = {}
    for candidate in (repo_root / "docs" / "spec.md", repo_root / "requirements.txt"):
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        current_reqs: list[str] = []
        for line in lines:
            found_reqs = _REQ_ID_RE.findall(line.upper())
            if line.lstrip().startswith("#") and found_reqs:
                current_reqs = found_reqs
            elif candidate.name == "requirements.txt" and found_reqs:
                current_reqs = found_reqs
            if not current_reqs:
                continue
            idents = {m.group(1) for m in _IDENT_RE.finditer(line) if m.group(1).lower() not in _STOP_WORDS}
            subtoks: set[str] = set()
            for ident in idents:
                subtoks.update(_split_subtokens(ident))
            for req_id in current_reqs:
                id_set, sub_set = req_map.setdefault(req_id, (set(), set()))
                id_set.update(idents)
                sub_set.update(subtoks)
    return req_map


def _extract_task_signals(
    task_text: str,
    spec_req_map: dict[str, tuple[set[str], set[str]]],
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Extract ``(req_ids, identifiers, subtokens, path_tokens)`` from ``task_text``."""
    req_ids = {r.upper() for r in _REQ_ID_RE.findall(task_text)}
    identifiers: set[str] = set()
    subtokens: set[str] = set()
    for match in _IDENT_RE.finditer(task_text):
        tok = match.group(1)
        if tok.lower() in _STOP_WORDS:
            continue
        identifiers.add(tok)
        subtokens.update(_split_subtokens(tok))

    for req_id in req_ids:
        parts = req_id.split("-")
        for part in parts[1:]:
            if len(part) >= 3 and not part.isdigit():
                subtokens.add(part.lower())
        if req_id in spec_req_map:
            spec_idents, spec_subs = spec_req_map[req_id]
            identifiers.update(spec_idents)
            subtokens.update(spec_subs)

    path_tokens: set[str] = set()
    for m in re.finditer(r"([A-Za-z0-9_./-]+\.(?:h|hpp|hh|hxx|c|cpp|cc|cxx))", task_text):
        path_tokens.add(m.group(1).lower())
        path_tokens.add(Path(m.group(1)).name.lower())
        path_tokens.add(Path(m.group(1)).stem.lower())

    return req_ids, identifiers, subtokens, path_tokens


def rank_file_indices(
    indices: Sequence[FileIndex],
    task_text: str,
    repo_root: Path,
) -> list[FileIndex]:
    """Rank indexed C/C++ files by relevance to ``task_text`` with deterministic tie-breaking."""
    spec_req_map = _extract_spec_req_map(repo_root) if task_text.strip() else {}
    req_ids, task_idents, task_subtokens, path_tokens = _extract_task_signals(task_text, spec_req_map)

    ranked: list[FileIndex] = []
    for fidx in indices:
        score = 0
        rel_low = fidx.rel_path.lower()
        rel_path_obj = Path(fidx.rel_path)
        stem_low = rel_path_obj.stem.lower()
        name_low = rel_path_obj.name.lower()

        if task_text.strip():
            # Path / filename stem match
            if rel_low in path_tokens or name_low in path_tokens or stem_low in path_tokens:
                score += 25

            # File raw identifiers & subtokens
            file_idents = {m.group(1) for m in _IDENT_RE.finditer(fidx.raw_text)}
            sym_names = {s.name.split("::")[-1] for s in fidx.symbols} | {s.name for s in fidx.symbols}
            brief_idents = {
                m.group(1)
                for text_part in [fidx.file_brief, *(s.brief for s in fidx.symbols)]
                for m in _IDENT_RE.finditer(text_part)
            }

            # Exact identifier matches: symbol definitions/declarations > brief > raw body
            sym_hits = task_idents & sym_names
            brief_hits = (task_idents & brief_idents) - sym_hits
            raw_hits = (task_idents & file_idents) - sym_hits - brief_hits
            score += 25 * len(sym_hits) + 15 * len(brief_hits) + 10 * len(raw_hits)

            # REQ-ID direct hits in file text
            file_reqs = {r.upper() for r in _REQ_ID_RE.findall(fidx.raw_text)}
            score += 25 * len(req_ids & file_reqs)

            # Subtoken overlap across path, symbols, and briefs
            file_subtokens: set[str] = set(_split_subtokens(stem_low))
            for ident in (sym_names | brief_idents):
                file_subtokens.update(_split_subtokens(ident))
            score += 2 * len(task_subtokens & file_subtokens)

        fidx.score = score
        ranked.append(fidx)

    def _sort_key(item: FileIndex) -> tuple[int, int, int, str]:
        """Sort descending by score, then non-test before test, header before source, then path."""
        p = Path(item.rel_path)
        is_test = 1 if ("tests/" in item.rel_path or p.stem.startswith("test_")) else 0
        is_source = 0 if p.suffix.lower() in _HEADER_EXTS else 1
        return (-item.score, is_test, is_source, item.rel_path)

    ranked.sort(key=_sort_key)
    return ranked


def discover_repo_files(repo_root: Path) -> tuple[list[str], list[str]]:
    """Return deterministic sorted ``(all_files, cpp_files)`` relative to ``repo_root``."""
    all_files: list[str] = []
    cpp_files: list[str] = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        rel_str = rel.as_posix()
        all_files.append(rel_str)
        if path.suffix.lower() in _CPP_EXTS:
            cpp_files.append(rel_str)
    return all_files, cpp_files


def _format_symbol_line(sym: SymbolEntry) -> str:
    """Format one symbol entry as a concise Markdown bullet line."""
    sig = sym.signature
    if sym.brief:
        return f"- `{sig}` — {sym.brief}"
    return f"- `{sig}`"


def _fit_within_budget(text: str, budget: int) -> str:
    """Guarantee that ``estimate_tokens(text) <= budget`` by trimming trailing lines if needed."""
    if budget <= 0 or estimate_tokens(text) <= budget:
        return text
    max_chars = max(0, budget * 4)
    marker = "\n... (truncated to budget)\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    lines = text.splitlines(keepends=True)
    acc: list[str] = []
    used = 0
    limit = max_chars - len(marker)
    for ln in lines:
        if used + len(ln) > limit:
            break
        acc.append(ln)
        used += len(ln)
    trimmed = "".join(acc).rstrip() + marker
    if estimate_tokens(trimmed) > budget:
        trimmed = trimmed[:max_chars]
    return trimmed


def generate_repomap(
    repo_root: Path | str,
    *,
    task_text: str = "",
    budget: int = DEFAULT_BUDGET_TOKENS,
    include_title: bool = True,
    use_ctags: bool = False,
) -> str:
    """Build a deterministic, ranked, token-budgeted Markdown code index for ``repo_root``."""
    root = Path(repo_root).resolve()
    all_files, cpp_files = discover_repo_files(root)

    indices: list[FileIndex] = [extract_cpp_file_index(root / rel, rel) for rel in cpp_files]
    if use_ctags:
        ctags_map = _collect_ctags_symbols(root, cpp_files)
        for fidx in indices:
            existing_names = {s.name for s in fidx.symbols}
            for tag_sym in ctags_map.get(fidx.rel_path, []):
                if tag_sym.name not in existing_names:
                    fidx.symbols.append(tag_sym)
                    existing_names.add(tag_sym.name)

    ranked = rank_file_indices(indices, task_text, root)

    sections: list[str] = []
    if include_title:
        sections.extend(["# Code Map", ""])

    # 1. File Tree (budget-aware: reserve at least ~60% for symbols when possible)
    tree_budget = max(40, budget // 3)
    tree_lines: list[str] = ["## File Tree", ""]
    omitted_tree = 0
    for idx_f, rel in enumerate(all_files):
        candidate_line = f"- `{rel}`"
        tentative = "\n".join([*sections, *tree_lines, candidate_line, ""])
        if estimate_tokens(tentative) > tree_budget and idx_f > 0:
            omitted_tree = len(all_files) - idx_f
            break
        tree_lines.append(candidate_line)
    if omitted_tree > 0:
        tree_lines.append(f"- ... ({omitted_tree} more files)")
    tree_lines.append("")
    sections.extend(tree_lines)

    # 2. Ranked symbols per file within remaining token budget
    sections.extend(["## Symbols", ""])
    trunc_marker = "\n... (truncated to budget)\n"
    marker_tokens = estimate_tokens(trunc_marker)

    any_symbol_written = False
    truncated = False
    for fidx in ranked:
        if not fidx.symbols and not fidx.file_brief:
            continue
        header_lines = [f"### `{fidx.rel_path}`"]
        if fidx.file_brief:
            header_lines.append(f"_{fidx.file_brief}_")
        sym_lines = [_format_symbol_line(s) for s in fidx.symbols]
        block_lines = [*header_lines, *sym_lines, ""]

        current_text = "\n".join(sections)
        full_candidate = "\n".join([*sections, *block_lines])
        if estimate_tokens(full_candidate) + marker_tokens <= budget:
            sections.extend(block_lines)
            any_symbol_written = True
            continue

        # Partial fit for this file if at least header + one symbol fits
        header_candidate = "\n".join([*sections, *header_lines])
        if estimate_tokens(header_candidate) + marker_tokens >= budget:
            truncated = True
            break

        partial_lines = list(header_lines)
        added_sym = False
        for sline in sym_lines:
            cand = "\n".join([*sections, *partial_lines, sline, ""])
            if estimate_tokens(cand) + marker_tokens <= budget:
                partial_lines.append(sline)
                added_sym = True
            else:
                truncated = True
                break
        if added_sym or fidx.file_brief:
            partial_lines.append("")
            sections.extend(partial_lines)
            any_symbol_written = True
        if truncated or len(partial_lines) < len(block_lines):
            truncated = True
            break

    if not any_symbol_written and not any(f.symbols or f.file_brief for f in ranked):
        sections.append("_No C/C++ symbols found._")
        sections.append("")

    output = "\n".join(sections).rstrip() + "\n"
    if truncated:
        output = output.rstrip() + trunc_marker
    return _fit_within_budget(output, budget)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/repomap.py``."""
    parser = argparse.ArgumentParser(
        prog="repomap.py",
        description="Build a deterministic, ranked, token-budgeted C/C++ Markdown code map.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to repository root directory (default: .).",
    )
    parser.add_argument(
        "--task",
        default="",
        help="Optional path to task or brief Markdown file for relevance ranking.",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET_TOKENS,
        help=f"Maximum estimated token budget (default: {DEFAULT_BUDGET_TOKENS}).",
    )
    parser.add_argument(
        "--ctags",
        action="store_true",
        help="Optionally supplement stdlib regex parsing with ctags if installed.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/repomap.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.budget < 1:
        print("error: --budget must be >= 1", file=sys.stderr)
        return 2

    repo_root = Path(args.repo)
    if not repo_root.is_dir():
        print(f"error: repository directory not found: {repo_root}", file=sys.stderr)
        return 2

    task_text = ""
    if args.task:
        task_path = Path(args.task)
        if not task_path.is_file():
            print(f"error: task file not found: {task_path}", file=sys.stderr)
            return 2
        try:
            task_text = task_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: failed to read task file '{task_path}': {exc}", file=sys.stderr)
            return 1

    try:
        markdown = generate_repomap(
            repo_root,
            task_text=task_text,
            budget=args.budget,
            use_ctags=bool(args.ctags),
        )
    except OSError as exc:
        print(f"error: failed to index repository '{repo_root}': {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
