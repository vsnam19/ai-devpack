#!/usr/bin/env python3
# memory.py - Bounded, auditable project memory store, proposal parser, and retrieval engine.
#
# Usage:
#   Imported by runner/devpack_memory.py, runner/agent_exec.py, runner/devpack_dispatch.py,
#   and runner/devpack_run.py.
#
# Exit codes (when invoked directly with --help):
#   0 - Help text printed
#   2 - Invalid arguments
"""Bounded, deterministic, auditable project memory per SPEC_v02 section 5.5."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import uuid

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from enforcement.checks import secret_pii_check  # noqa: E402

VALID_TYPES: tuple[str, ...] = ("decision", "fact", "handoff", "known-gap")
VALID_STATUSES: tuple[str, ...] = ("active", "pending", "superseded", "expired")
AUTO_ACCEPT_TYPES: tuple[str, ...] = ("handoff", "known-gap")
HUMAN_APPROVAL_TYPES: tuple[str, ...] = ("decision", "fact")
APPROVAL_REQUIRED_TYPES: tuple[str, ...] = ("decision", "fact")

DEFAULT_TOP_K = 5
DEFAULT_BUDGET_TOKENS = 800

_VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REQ_ID_RE = re.compile(r"\bREQ-[A-Z0-9]+(?:-[A-Z0-9]+)+\b", re.IGNORECASE)
_FILE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:hpp|cpp|hh|cc|h|c|py|md|toml|sh|json)\b"
)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_CAMEL_OR_SNAKE_RE = re.compile(r"\b(?:[a-z]+_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b")

_BOILERPLATE_FILES = frozenset(
    {
        "docs/spec.md",
        "spec.md",
        "agents.md",
        "cmakelists.txt",
        "build_and_test.sh",
        "stop_report.md",
        "review.md",
        "preflight.toml",
        "requirements.txt",
    }
)
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "against",
        "all",
        "allowed",
        "also",
        "and",
        "any",
        "are",
        "before",
        "brief",
        "build",
        "but",
        "can",
        "changes",
        "check",
        "code",
        "conditions",
        "context",
        "current",
        "docs",
        "documents",
        "does",
        "each",
        "edit",
        "else",
        "expected",
        "fail",
        "file",
        "files",
        "fix",
        "for",
        "from",
        "goal",
        "implement",
        "implementer",
        "include",
        "input",
        "into",
        "investigate",
        "keep",
        "lines",
        "memory",
        "must",
        "never",
        "new",
        "not",
        "note",
        "only",
        "out",
        "output",
        "pass",
        "read",
        "report",
        "requirement",
        "requirements",
        "return",
        "review",
        "reviewer",
        "role",
        "rule",
        "run",
        "scope",
        "see",
        "set",
        "should",
        "spec",
        "src",
        "state",
        "step",
        "stop",
        "summary",
        "task",
        "test",
        "tests",
        "than",
        "that",
        "the",
        "their",
        "then",
        "this",
        "true",
        "false",
        "until",
        "update",
        "use",
        "used",
        "using",
        "value",
        "values",
        "when",
        "where",
        "which",
        "while",
        "with",
        "without",
        "workdir",
        "write",
    }
)

# Extra credential/PII patterns beyond secret_pii_check built-ins.
# Built from fragments where needed to satisfy repository leak checks.
_HOST_HOME_PREFIX = "/" + "ho" + "me/"
_EXTRA_SECRET_PII_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prefixed-api-token", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("pii-email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("host-home-path", re.compile(re.escape(_HOST_HOME_PREFIX) + r"[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+")),
)
_SECRET_ENV_SUFFIXES = ("_AUTH_TOKEN", "_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")

_PROPOSALS_HEADING_RE = re.compile(r"^##\s+Memory\s+proposals\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL | re.IGNORECASE)
_STRIP_SECTIONS_RE = re.compile(
    r"^##\s+(?:Known\s+mistakes\s+for\s+this\s+engine|Relevant\s+project\s+memory|Memory\s+proposals|Code\s+quality\s+checklist)\b.*?(?=^##\s+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


class MemoryValidationError(ValueError):
    """Raised when a memory entry fails schema, scope, or secret/PII validation."""


class SecretPiiRejectedError(MemoryValidationError):
    """Raised when a memory write contains forbidden secrets or PII."""


class MemoryNotFoundError(KeyError):
    """Raised when a requested memory entry ID does not exist."""


@dataclass
class MemoryEntry:
    """Structured representation of a `.devpack/memory/<scope>/<id>.md` entry."""

    id: str
    entry_type: str
    scope: str
    tags: list[str] = field(default_factory=list)
    created: str = ""
    source: str = "human"
    supersedes: list[str] = field(default_factory=list)
    expires: str = ""
    status: str = "active"
    body: str = ""
    title: str = ""
    path: Path | None = None
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize entry metadata and body to a JSON-friendly dictionary."""
        return {
            "id": self.id,
            "type": self.entry_type,
            "scope": self.scope,
            "tags": list(self.tags),
            "created": self.created,
            "source": self.source,
            "supersedes": list(self.supersedes),
            "expires": self.expires,
            "status": self.status,
            "title": self.title,
            "body": self.body,
            "path": str(self.path) if self.path else "",
            "sha256": self.sha256,
        }


@dataclass
class QueryMatch:
    """Explanation record for a candidate memory entry evaluated during retrieval."""

    entry: MemoryEntry
    included: bool
    reason: str
    specificity_score: int = 0
    scope_specificity: int = 0
    matched_reqs: list[str] = field(default_factory=list)
    matched_modules: list[str] = field(default_factory=list)
    matched_files: list[str] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize match explanation to a JSON-friendly dictionary."""
        return {
            "id": self.entry.id,
            "type": self.entry.entry_type,
            "scope": self.entry.scope,
            "status": self.entry.status,
            "included": self.included,
            "reason": self.reason,
            "specificity_score": self.specificity_score,
            "scope_specificity": self.scope_specificity,
            "matched_reqs": list(self.matched_reqs),
            "matched_modules": list(self.matched_modules),
            "matched_files": list(self.matched_files),
            "matched_symbols": list(self.matched_symbols),
            "tokens": self.tokens,
            "created": self.entry.created,
            "source": self.entry.source,
            "sha256": self.entry.sha256,
        }


@dataclass
class QueryResult:
    """Result of deterministic memory retrieval within top-k and token budget limits."""

    selected: list[MemoryEntry]
    matches: list[QueryMatch]
    total_tokens: int
    budget_tokens: int
    top_k: int
    formatted_prompt: str
    ids: list[str]
    hashes: dict[str, str]


@dataclass
class MemoryProposal:
    """Parsed candidate memory entry from an agent's `## Memory proposals` block."""

    entry_type: str
    scope: str = "project"
    tags: list[str] = field(default_factory=list)
    body: str = ""
    title: str = ""
    supersedes: list[str] = field(default_factory=list)
    expires: str = ""
    id_hint: str = ""


@dataclass
class ProposalProcessResult:
    """Summary of proposals auto-accepted, queued as pending, or rejected."""

    accepted: list[MemoryEntry] = field(default_factory=list)
    pending: list[MemoryEntry] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize proposal handling summary for run metadata."""
        return {
            "accepted": [e.id for e in self.accepted],
            "pending": [e.id for e in self.pending],
            "rejected": list(self.rejected),
        }


def memory_root_for(workdir: Path | str) -> Path:
    """Return the canonical `.devpack/memory` directory for ``workdir``."""
    return Path(workdir) / ".devpack" / "memory"


def estimate_tokens(text: str) -> int:
    """Return a deterministic stdlib token estimate (~4 characters per token)."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def now_utc_iso(now: datetime | None = None) -> str:
    """Return a normalized UTC ISO-8601 timestamp ending in ``Z``."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso_datetime(raw: str, *, end_of_day_for_date: bool = False) -> datetime | None:
    """Parse an ISO-8601 date or timestamp into a UTC-aware ``datetime``."""
    cleaned = raw.strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        suffix = "T23:59:59+00:00" if end_of_day_for_date else "T00:00:00+00:00"
        cleaned = cleaned + suffix
    elif cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_entry_id(entry_id: str) -> str:
    """Validate and return a safe memory entry ID."""
    cleaned = str(entry_id).strip()
    if not _VALID_ID_RE.match(cleaned) or ".." in cleaned:
        raise MemoryValidationError(f"Invalid memory id: {entry_id!r}")
    return cleaned


def validate_entry_type(entry_type: str) -> str:
    """Validate and return one of ``decision|fact|handoff|known-gap``."""
    cleaned = str(entry_type).strip().lower()
    if cleaned not in VALID_TYPES:
        raise MemoryValidationError(
            f"Invalid memory type {entry_type!r}; expected one of {', '.join(VALID_TYPES)}"
        )
    return cleaned


def validate_status(status: str) -> str:
    """Validate and return one of ``active|pending|superseded|expired``."""
    cleaned = str(status).strip().lower()
    if cleaned not in VALID_STATUSES:
        raise MemoryValidationError(
            f"Invalid memory status {status!r}; expected one of {', '.join(VALID_STATUSES)}"
        )
    return cleaned


def validate_scope(scope: str) -> str:
    """Validate a scope string (``project``, ``module:<path>``, ``role:<role>``, ``engine:<name>``)."""
    cleaned = str(scope).strip()
    if not cleaned or "\x00" in cleaned or "\\" in cleaned:
        raise MemoryValidationError(f"Invalid memory scope: {scope!r}")
    if cleaned == "project":
        return "project"
    for prefix in ("module:", "role:", "engine:"):
        if cleaned.startswith(prefix):
            val = cleaned[len(prefix) :].strip()
            if not val or val.startswith("/") or ".." in Path(val).parts:
                raise MemoryValidationError(f"Invalid memory scope target: {scope!r}")
            if prefix in ("role:", "engine:") and "/" in val:
                raise MemoryValidationError(f"Invalid {prefix} scope (no '/' allowed): {scope!r}")
            return prefix + val.strip("/")
    raise MemoryValidationError(
        f"Invalid memory scope {scope!r}; expected project, module:<path>, role:<role>, or engine:<name>"
    )


def scope_dir_name(scope: str) -> str:
    """Map a validated scope string to a safe single-level directory name under ``.devpack/memory/``."""
    norm = validate_scope(scope)
    if norm.startswith("module:"):
        target = norm[len("module:") :].replace("/", "__")
        return f"module:{target}"
    return norm


def generate_memory_id(prefix: str = "mem", *, now: datetime | None = None) -> str:
    """Generate a unique, filesystem-safe memory entry identifier."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix.strip()) or "mem"
    return f"{safe_prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def _collect_env_secret_values() -> list[str]:
    """Collect active auth/secret environment variable values to prevent accidental storage."""
    values: list[str] = []
    for key, val in os.environ.items():
        upper = key.upper()
        if upper in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "DEVPACK_LOCAL_AUTH_TOKEN") or any(
            upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES
        ):
            cleaned = val.strip()
            if len(cleaned) >= 4:
                values.append(cleaned)
    return values


def check_secret_pii(
    text: str,
    *,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> list[str]:
    """Return a list of violated secret/PII rule IDs found in ``text`` without echoing secrets."""
    if not text:
        return []
    violations: list[str] = []
    seen: set[str] = set()

    def _add(rule_id: str) -> None:
        """Record unique rule violation ID."""
        if rule_id not in seen:
            seen.add(rule_id)
            violations.append(rule_id)

    for lit in list(extra_secrets) + _collect_env_secret_values():
        cleaned = str(lit).strip()
        if len(cleaned) >= 4 and cleaned in text:
            _add("env-secret-value")

    rules: list[tuple[str, re.Pattern[str]]] = list(secret_pii_check.get_builtin_rules())
    rules.extend(_EXTRA_SECRET_PII_RULES)

    ext_path_raw = str(patterns_file) if patterns_file else os.environ.get("DEVPACK_SECRET_PATTERNS", "").strip()
    if ext_path_raw:
        ext_path = Path(ext_path_raw)
        if ext_path.is_file():
            loaded, err = secret_pii_check.load_external_patterns(ext_path)
            if loaded is None:
                raise MemoryValidationError(f"Failed to load secret patterns file: {err}")
            rules.extend(loaded)

    for line in text.splitlines():
        for rule_id, rx in rules:
            if rx.search(line):
                _add(rule_id)

    return violations


def _unquote(value: str) -> str:
    """Strip matching surrounding single or double quotes from a scalar YAML value."""
    s = value.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _parse_list_value(raw: str) -> list[str]:
    """Parse an inline ``[a, b]`` or comma-separated YAML list."""
    cleaned = raw.strip()
    if not cleaned or cleaned in ("[]", "null", "~"):
        return []
    if cleaned.startswith("[") and cleaned.endswith("]"):
        inner = cleaned[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part) for part in inner.split(",") if _unquote(part)]
    return [_unquote(part) for part in cleaned.split(",") if _unquote(part)]


def _parse_frontmatter(text: str, source_path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Split YAML-subset frontmatter and markdown body from a memory entry file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise MemoryValidationError(f"Missing YAML frontmatter delimiter in {source_path or '<memory>'}")
    end_idx = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx < 0:
        raise MemoryValidationError(f"Unterminated YAML frontmatter in {source_path or '<memory>'}")

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in lines[1:end_idx]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line.lstrip().startswith("- ") and current_list_key is not None:
            item = _unquote(raw_line.lstrip()[2:].strip())
            if item:
                meta.setdefault(current_list_key, []).append(item)
            continue
        current_list_key = None
        if ":" not in raw_line:
            continue
        key, val = raw_line.split(":", 1)
        key = key.strip()
        val_stripped = val.strip()
        if key in ("tags", "supersedes"):
            if not val_stripped:
                meta[key] = []
                current_list_key = key
            elif val_stripped.startswith("["):
                meta[key] = _parse_list_value(val_stripped)
            else:
                unq = _unquote(val_stripped)
                if unq in ("", "null", "~"):
                    meta[key] = []
                else:
                    meta[key] = _parse_list_value(unq)
        else:
            unq = _unquote(val_stripped)
            meta[key] = "" if unq in ("null", "~") else unq

    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


def _extract_title(meta: Mapping[str, Any], body: str) -> str:
    """Return explicit frontmatter title or first Markdown heading if present."""
    title = str(meta.get("title", "")).strip()
    if title:
        return title
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def parse_memory_file(path: Path) -> MemoryEntry:
    """Read and parse a `.devpack/memory/<scope>/<id>.md` file into ``MemoryEntry``."""
    raw_bytes = path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8", errors="replace")
    meta, body = _parse_frontmatter(text, source_path=path)

    raw_id = str(meta.get("id", "")).strip() or path.stem
    entry_id = validate_entry_id(raw_id)
    entry_type = validate_entry_type(str(meta.get("type", "")))
    scope = validate_scope(str(meta.get("scope", "project")))
    status = validate_status(str(meta.get("status", "active")))

    raw_tags = meta.get("tags", [])
    tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []

    raw_sup = meta.get("supersedes", [])
    supersedes = [str(s).strip() for s in raw_sup if str(s).strip()] if isinstance(raw_sup, list) else []

    return MemoryEntry(
        id=entry_id,
        entry_type=entry_type,
        scope=scope,
        tags=tags,
        created=str(meta.get("created", "")).strip(),
        source=str(meta.get("source", "human")).strip() or "human",
        supersedes=supersedes,
        expires=str(meta.get("expires", "")).strip(),
        status=status,
        body=body,
        title=_extract_title(meta, body),
        path=path,
        sha256=sha256,
    )


def _yaml_scalar(value: str) -> str:
    """Format a scalar string safely for YAML-subset frontmatter."""
    cleaned = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return '""'
    if any(ch in cleaned for ch in (":", "#", "[", "]", "{", "}", '"', "'", ",")) or cleaned in ("null", "~", "true", "false"):
        escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return cleaned


def serialize_memory_entry(entry: MemoryEntry) -> str:
    """Serialize ``MemoryEntry`` to Markdown with YAML frontmatter per SPEC §5.5."""
    lines = [
        "---",
        f"id: {_yaml_scalar(entry.id)}",
        f"type: {_yaml_scalar(entry.entry_type)}",
        f"scope: {_yaml_scalar(entry.scope)}",
    ]
    if entry.tags:
        lines.append("tags:")
        for tag in entry.tags:
            lines.append(f"  - {_yaml_scalar(tag)}")
    else:
        lines.append("tags: []")
    lines.append(f"created: {_yaml_scalar(entry.created)}")
    lines.append(f"source: {_yaml_scalar(entry.source)}")
    if len(entry.supersedes) == 0:
        lines.append('supersedes: ""')
    elif len(entry.supersedes) == 1:
        lines.append(f"supersedes: {_yaml_scalar(entry.supersedes[0])}")
    else:
        lines.append("supersedes:")
        for sid in entry.supersedes:
            lines.append(f"  - {_yaml_scalar(sid)}")
    lines.append(f"expires: {_yaml_scalar(entry.expires)}")
    lines.append(f"status: {_yaml_scalar(entry.status)}")
    if entry.title and not entry.body.lstrip().startswith("# "):
        lines.append(f"title: {_yaml_scalar(entry.title)}")
    lines.append("---")
    lines.append("")
    body_text = entry.body.strip()
    if entry.title and not body_text.lstrip().startswith("# "):
        lines.append(f"# {entry.title}")
        if body_text:
            lines.append("")
    if body_text:
        lines.append(body_text)
    lines.append("")
    return "\n".join(lines)


def load_memory_entries(memory_root: Path | str) -> list[MemoryEntry]:
    """Load all valid `.md` memory entries beneath ``memory_root`` sorted deterministically."""
    root = Path(memory_root)
    if not root.is_dir():
        return []
    entries: list[MemoryEntry] = []
    for md_path in sorted(root.rglob("*.md")):
        if not md_path.is_file() or md_path.name.startswith("."):
            continue
        entries.append(parse_memory_file(md_path))
    entries.sort(key=lambda e: (e.created, e.id))
    return entries


def find_memory_entry(memory_root: Path | str, entry_id: str) -> MemoryEntry:
    """Return the ``MemoryEntry`` matching ``entry_id`` or raise ``MemoryNotFoundError``."""
    target = validate_entry_id(entry_id)
    for entry in load_memory_entries(memory_root):
        if entry.id == target:
            return entry
    raise MemoryNotFoundError(f"Memory entry not found: {target}")


def _write_entry_raw(
    target_path: Path,
    entry: MemoryEntry,
    *,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> MemoryEntry:
    """Validate, run secret/PII check, atomically write ``target_path``, and update sha256."""
    serialized = serialize_memory_entry(entry)
    violations = check_secret_pii(
        serialized,
        extra_secrets=extra_secrets,
        patterns_file=patterns_file,
    )
    if violations:
        raise SecretPiiRejectedError(
            f"Secret/PII check rejected memory entry '{entry.id}': {', '.join(violations)}"
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    raw_bytes = serialized.encode("utf-8")
    tmp_path.write_bytes(raw_bytes)
    os.replace(tmp_path, target_path)
    entry.path = target_path
    entry.sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return entry


def write_memory_entry(
    memory_root: Path | str,
    entry: MemoryEntry,
    *,
    overwrite: bool = False,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> MemoryEntry:
    """Validate, secret-check, and persist a ``MemoryEntry`` under ``memory_root/<scope>/<id>.md``."""
    root = Path(memory_root)
    entry.id = validate_entry_id(entry.id)
    entry.entry_type = validate_entry_type(entry.entry_type)
    entry.scope = validate_scope(entry.scope)
    entry.status = validate_status(entry.status)
    entry.tags = [str(t).strip() for t in entry.tags if str(t).strip()]
    entry.supersedes = [validate_entry_id(s) for s in entry.supersedes if str(s).strip()]
    if not entry.created:
        entry.created = now_utc_iso()
    if entry.expires and parse_iso_datetime(entry.expires, end_of_day_for_date=True) is None:
        raise MemoryValidationError(f"Invalid expires timestamp: {entry.expires!r}")

    target_dir = root / scope_dir_name(entry.scope)
    target_path = target_dir / f"{entry.id}.md"

    existing = {e.id: e for e in load_memory_entries(root)}
    if not overwrite and entry.id in existing:
        raise MemoryValidationError(f"Memory entry already exists: {entry.id}")
    if overwrite and entry.id in existing and existing[entry.id].path and existing[entry.id].path != target_path:
        existing[entry.id].path.unlink(missing_ok=True)

    written = _write_entry_raw(
        target_path,
        entry,
        extra_secrets=extra_secrets,
        patterns_file=patterns_file,
    )

    # When an active entry declares supersedes, mark target entries superseded on disk.
    if written.status == "active" and written.supersedes:
        for old_id in written.supersedes:
            if old_id in existing and old_id != written.id:
                old_entry = existing[old_id]
                if old_entry.status != "superseded" and old_entry.path is not None:
                    old_entry.status = "superseded"
                    _write_entry_raw(
                        old_entry.path,
                        old_entry,
                        extra_secrets=extra_secrets,
                        patterns_file=patterns_file,
                    )

    return written


def add_memory_entry(
    memory_root: Path | str,
    *,
    entry_type: str,
    scope: str = "project",
    body: str,
    tags: Sequence[str] = (),
    source: str = "human",
    supersedes: Sequence[str] = (),
    expires: str = "",
    status: str = "active",
    entry_id: str = "",
    title: str = "",
    created: str = "",
    overwrite: bool = False,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> MemoryEntry:
    """Create and persist a new memory entry after schema and secret/PII validation."""
    if not str(body).strip():
        raise MemoryValidationError("Memory entry body must not be empty")
    resolved_id = validate_entry_id(entry_id) if entry_id else generate_memory_id("mem")
    entry = MemoryEntry(
        id=resolved_id,
        entry_type=validate_entry_type(entry_type),
        scope=validate_scope(scope),
        tags=[str(t).strip() for t in tags if str(t).strip()],
        created=created.strip() or now_utc_iso(),
        source=str(source).strip() or "human",
        supersedes=[validate_entry_id(s) for s in supersedes if str(s).strip()],
        expires=str(expires).strip(),
        status=validate_status(status),
        body=str(body).strip(),
        title=str(title).strip(),
    )
    return write_memory_entry(
        memory_root,
        entry,
        overwrite=overwrite,
        extra_secrets=extra_secrets,
        patterns_file=patterns_file,
    )


def approve_memory_entry(
    memory_root: Path | str,
    entry_id: str,
    *,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> MemoryEntry:
    """Approve a ``pending`` memory entry, transitioning it to ``active``."""
    entry = find_memory_entry(memory_root, entry_id)
    if entry.status in ("superseded", "expired"):
        raise MemoryValidationError(f"Cannot approve {entry.status} memory entry '{entry.id}'")
    entry.status = "active"
    return write_memory_entry(
        memory_root,
        entry,
        overwrite=True,
        extra_secrets=extra_secrets,
        patterns_file=patterns_file,
    )


def supersede_memory_entry(
    memory_root: Path | str,
    old_id: str,
    *,
    by_id: str = "",
) -> MemoryEntry:
    """Mark ``old_id`` as ``superseded``, optionally verifying/linking ``by_id``."""
    entry = find_memory_entry(memory_root, old_id)
    if by_id:
        new_entry = find_memory_entry(memory_root, by_id)
        if entry.id not in new_entry.supersedes:
            new_entry.supersedes.append(entry.id)
            write_memory_entry(memory_root, new_entry, overwrite=True)
    entry.status = "superseded"
    return write_memory_entry(memory_root, entry, overwrite=True)


def expire_memory_entry(memory_root: Path | str, entry_id: str) -> MemoryEntry:
    """Mark a single memory entry ``expired``."""
    entry = find_memory_entry(memory_root, entry_id)
    entry.status = "expired"
    return write_memory_entry(memory_root, entry, overwrite=True)


def is_entry_expired(entry: MemoryEntry, *, now: datetime | None = None) -> bool:
    """Return True if ``entry.status == 'expired'`` or ``entry.expires`` is in the past."""
    if entry.status == "expired":
        return True
    if not entry.expires:
        return False
    exp_dt = parse_iso_datetime(entry.expires, end_of_day_for_date=True)
    if exp_dt is None:
        return False
    ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ref_now > exp_dt


def expire_stale_entries(memory_root: Path | str, *, now: datetime | None = None) -> list[MemoryEntry]:
    """Transition all active/pending entries whose ``expires`` has passed to ``expired``."""
    changed: list[MemoryEntry] = []
    for entry in load_memory_entries(memory_root):
        if entry.status in ("active", "pending") and entry.expires and is_entry_expired(entry, now=now):
            entry.status = "expired"
            changed.append(write_memory_entry(memory_root, entry, overwrite=True))
    return changed


def _strip_injected_sections(text: str) -> str:
    """Remove appended checklist/profile/memory/proposal boilerplate before signal extraction."""
    return _STRIP_SECTIONS_RE.sub("", text)


def extract_task_signals(task_text: str) -> dict[str, set[str]]:
    """Extract normalized REQ IDs, file paths, basenames, modules, and symbols from a task brief."""
    cleaned = _strip_injected_sections(task_text)
    req_ids: set[str] = {m.group(0).upper() for m in _REQ_ID_RE.finditer(cleaned)}

    files: set[str] = set()
    basenames: set[str] = set()
    modules: set[str] = set()
    for m in _FILE_PATH_RE.finditer(cleaned):
        raw_path = m.group(0).strip().lstrip("./")
        norm = raw_path.lower()
        if norm in _BOILERPLATE_FILES:
            continue
        files.add(norm)
        p = Path(norm)
        if p.name not in _BOILERPLATE_FILES:
            basenames.add(p.name)
            if len(p.stem) >= 3 and p.stem not in _STOPWORDS:
                modules.add(p.stem)

    symbols: set[str] = set()
    for m in _CAMEL_OR_SNAKE_RE.finditer(cleaned):
        tok = m.group(0).lower()
        if tok not in _STOPWORDS and len(tok) >= 3:
            symbols.add(tok)
    for m in _IDENT_RE.finditer(cleaned):
        tok = m.group(0).lower()
        if tok not in _STOPWORDS and len(tok) >= 4:
            symbols.add(tok)

    return {
        "req_ids": req_ids,
        "files": files,
        "basenames": basenames,
        "modules": modules,
        "symbols": symbols,
    }


def _scope_specificity(scope: str) -> int:
    """Return numeric specificity rank for a scope (higher = more specific)."""
    if scope.startswith(("role:", "engine:")):
        return 3
    if scope.startswith("module:"):
        return 2
    return 1


def _is_scope_visible(
    entry: MemoryEntry,
    *,
    role: str | None,
    engine: str | None,
    reviewer_sees_handoffs: bool,
    role_scopes: Mapping[str, Sequence[str]] | None,
) -> tuple[bool, str]:
    """Check whether ``entry`` is visible to ``(role, engine)`` under scope isolation rules."""
    norm_role = (role or "").strip().lower()
    norm_engine = (engine or "").strip().lower()
    extra_scopes = set()
    if role_scopes and norm_role and norm_role in role_scopes:
        extra_scopes.update(str(s).strip() for s in role_scopes[norm_role])

    if entry.scope.startswith("role:"):
        target_role = entry.scope[len("role:") :].strip().lower()
        if norm_role != target_role and entry.scope not in extra_scopes:
            return False, "role_excluded"

    if entry.scope.startswith("engine:"):
        target_engine = entry.scope[len("engine:") :].strip().lower()
        if norm_engine != target_engine and entry.scope not in extra_scopes:
            return False, "scope_excluded"

    if norm_role == "reviewer" and entry.entry_type == "handoff":
        if entry.scope != "role:reviewer" and not reviewer_sees_handoffs and entry.scope not in extra_scopes:
            return False, "role_excluded"

    return True, ""


def _match_entry(entry: MemoryEntry, signals: Mapping[str, set[str]]) -> tuple[int, list[str], list[str], list[str], list[str]]:
    """Compute deterministic match score and matched signal lists for ``entry``."""
    matched_reqs: list[str] = []
    matched_modules: list[str] = []
    matched_files: list[str] = []
    matched_symbols: list[str] = []

    if entry.scope.startswith("module:"):
        mod_path = entry.scope[len("module:") :].strip().lstrip("./").lower()
        mod_p = Path(mod_path)
        if (
            mod_path in signals["files"]
            or mod_p.name in signals["files"]
            or mod_p.name in signals["basenames"]
            or mod_p.stem in signals["modules"]
            or mod_p.stem in signals["symbols"]
        ):
            matched_modules.append(entry.scope)

    for raw_tag in entry.tags:
        tag = raw_tag.strip()
        if not tag:
            continue
        upper = tag.upper()
        lower = tag.lower().lstrip("./")
        if upper.startswith("REQ-"):
            if upper in signals["req_ids"]:
                matched_reqs.append(upper)
            continue
        if "/" in lower or any(lower.endswith(ext) for ext in (".hpp", ".cpp", ".hh", ".cc", ".h", ".c", ".py", ".md", ".toml")):
            tag_p = Path(lower)
            if (
                lower in signals["files"]
                or tag_p.name in signals["basenames"]
                or (tag_p.stem in signals["modules"] and tag_p.stem not in _STOPWORDS)
            ):
                matched_files.append(tag)
            continue
        if lower in ("*", "all"):
            matched_symbols.append(tag)
            continue
        if lower not in _STOPWORDS and (lower in signals["symbols"] or lower in signals["modules"]):
            matched_symbols.append(tag)

    score = (
        len(matched_reqs) * 40
        + len(matched_modules) * 35
        + len(matched_files) * 30
        + len(matched_symbols) * 20
    )
    if score > 0:
        score += _scope_specificity(entry.scope) * 5
    return score, matched_reqs, matched_modules, matched_files, matched_symbols


def format_entry_block(entry: MemoryEntry) -> str:
    """Format a single memory entry for prompt injection, citing source and metadata."""
    tags_str = ", ".join(entry.tags) if entry.tags else "(none)"
    meta_line = (
        f"- **[{entry.id}]** (type: `{entry.entry_type}`, scope: `{entry.scope}`, "
        f"source: `{entry.source}`, created: `{entry.created or 'unknown'}`, tags: `{tags_str}`)"
    )
    body_lines = [line.rstrip() for line in entry.body.strip().splitlines()]
    indented = "\n".join(f"  {line}" if line else "" for line in body_lines)
    return f"{meta_line}\n{indented}"


def format_injected_memory(entries: Sequence[MemoryEntry]) -> str:
    """Render the ``## Relevant project memory`` section with the stale-memory precedence rule."""
    if not entries:
        return ""
    header = (
        "## Relevant project memory\n\n"
        "> Note: Project memory entries are advisory and may be stale. Current repository code and "
        "specification (`docs/spec.md`) always take precedence on conflict; if any memory entry "
        "contradicts current code or spec, follow the code/spec and explicitly report the conflict.\n"
    )
    blocks = "\n\n".join(format_entry_block(entry) for entry in entries)
    return f"{header}\n{blocks}\n"


def retrieve_memory(
    memory_root: Path | str,
    task_text: str,
    *,
    role: str | None = None,
    engine: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    reviewer_sees_handoffs: bool = False,
    role_scopes: Mapping[str, Sequence[str]] | None = None,
    now: datetime | None = None,
) -> QueryResult:
    """Deterministically select up to ``top_k`` relevant active entries within ``budget_tokens``."""
    entries = load_memory_entries(memory_root)
    if not entries or top_k <= 0 or budget_tokens <= 0:
        return QueryResult(
            selected=[],
            matches=[],
            total_tokens=0,
            budget_tokens=max(0, budget_tokens),
            top_k=max(0, top_k),
            formatted_prompt="",
            ids=[],
            hashes={},
        )

    # Any non-pending entry that declares supersedes=<id> suppresses <id> even if <id>.md wasn't rewritten.
    superseded_ids: set[str] = set()
    for entry in entries:
        if entry.status in ("active", "superseded", "expired") and entry.supersedes:
            superseded_ids.update(entry.supersedes)

    signals = extract_task_signals(task_text)
    candidates: list[tuple[tuple[int, int, str, str], QueryMatch]] = []
    rejected_matches: list[QueryMatch] = []

    for entry in entries:
        if entry.status == "pending":
            rejected_matches.append(QueryMatch(entry=entry, included=False, reason="pending"))
            continue
        if entry.status == "superseded" or entry.id in superseded_ids:
            rejected_matches.append(QueryMatch(entry=entry, included=False, reason="superseded"))
            continue
        if is_entry_expired(entry, now=now):
            rejected_matches.append(QueryMatch(entry=entry, included=False, reason="expired"))
            continue

        visible, scope_reason = _is_scope_visible(
            entry,
            role=role,
            engine=engine,
            reviewer_sees_handoffs=reviewer_sees_handoffs,
            role_scopes=role_scopes,
        )
        if not visible:
            rejected_matches.append(QueryMatch(entry=entry, included=False, reason=scope_reason))
            continue

        score, reqs, mods, files, syms = _match_entry(entry, signals)
        if score <= 0:
            rejected_matches.append(
                QueryMatch(
                    entry=entry,
                    included=False,
                    reason="no_match",
                    scope_specificity=_scope_specificity(entry.scope),
                )
            )
            continue

        block_tokens = estimate_tokens(format_entry_block(entry))
        scope_spec = _scope_specificity(entry.scope)
        created_dt = parse_iso_datetime(entry.created)
        created_key = created_dt.isoformat() if created_dt else entry.created
        rank_key = (score, scope_spec, created_key, entry.id)
        candidates.append(
            (
                rank_key,
                QueryMatch(
                    entry=entry,
                    included=False,
                    reason="candidate",
                    specificity_score=score,
                    scope_specificity=scope_spec,
                    matched_reqs=reqs,
                    matched_modules=mods,
                    matched_files=files,
                    matched_symbols=syms,
                    tokens=block_tokens,
                ),
            )
        )

    candidates.sort(key=lambda item: item[0], reverse=True)

    header_overhead = estimate_tokens(
        "## Relevant project memory\n\n"
        "> Note: Project memory entries are advisory and may be stale. Current repository code and "
        "specification (`docs/spec.md`) always take precedence on conflict; if any memory entry "
        "contradicts current code or spec, follow the code/spec and explicitly report the conflict.\n\n"
    )

    selected: list[MemoryEntry] = []
    evaluated_matches: list[QueryMatch] = []
    used_tokens = 0

    for _, qm in candidates:
        if len(selected) >= top_k:
            qm.reason = "over_top_k"
            evaluated_matches.append(qm)
            continue
        needed = qm.tokens + (header_overhead if not selected else 1)
        if used_tokens + needed > budget_tokens:
            qm.reason = "over_budget"
            evaluated_matches.append(qm)
            continue
        qm.included = True
        qm.reason = "selected"
        selected.append(qm.entry)
        used_tokens += needed
        evaluated_matches.append(qm)

    all_matches = evaluated_matches + rejected_matches
    formatted = format_injected_memory(selected)
    actual_tokens = estimate_tokens(formatted) if selected else 0
    return QueryResult(
        selected=selected,
        matches=all_matches,
        total_tokens=actual_tokens,
        budget_tokens=budget_tokens,
        top_k=top_k,
        formatted_prompt=formatted,
        ids=[e.id for e in selected],
        hashes={e.id: e.sha256 for e in selected},
    )


def _extract_proposals_section(text: str) -> str:
    """Return the body under the last ``## Memory proposals`` heading in ``text``."""
    matches = list(_PROPOSALS_HEADING_RE.finditer(text))
    if not matches:
        return ""
    start = matches[-1].end()
    rest = text[start:]
    next_h2 = _NEXT_H2_RE.search(rest.lstrip("\n"))
    if next_h2 is not None:
        # Compute offset relative to rest
        leading_nl = len(rest) - len(rest.lstrip("\n"))
        rest = rest[: leading_nl + next_h2.start()]
    return rest.strip()


def _proposal_from_dict(raw: Mapping[str, Any]) -> MemoryProposal | None:
    """Convert a parsed mapping into a ``MemoryProposal`` if it has valid type and content."""
    raw_type = str(raw.get("type", "")).strip().lower()
    if not raw_type:
        return None
    body_val = ""
    for key in ("body", "summary", "content", "text", "description"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            body_val = val.strip()
            break
    title_val = str(raw.get("title", "")).strip()
    if not body_val and title_val:
        body_val = title_val
    if not body_val:
        return None

    raw_tags = raw.get("tags", [])
    if isinstance(raw_tags, str):
        tags = _parse_list_value(raw_tags)
    elif isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tags = []

    raw_sup = raw.get("supersedes", [])
    if isinstance(raw_sup, str):
        supersedes = _parse_list_value(raw_sup)
    elif isinstance(raw_sup, list):
        supersedes = [str(s).strip() for s in raw_sup if str(s).strip()]
    else:
        supersedes = []

    return MemoryProposal(
        entry_type=raw_type,
        scope=str(raw.get("scope", "project")).strip() or "project",
        tags=tags,
        body=body_val,
        title=title_val,
        supersedes=supersedes,
        expires=str(raw.get("expires", "")).strip(),
        id_hint=str(raw.get("id", "")).strip(),
    )


def parse_memory_proposals(text: str) -> list[MemoryProposal]:
    """Parse ``## Memory proposals`` from agent final text or ``STOP_REPORT.md``."""
    section = _extract_proposals_section(text)
    if not section:
        return []
    stripped_low = section.strip().lower()
    if stripped_low in ("none", "none.", "(none)", "n/a", "- none"):
        return []

    proposals: list[MemoryProposal] = []
    # 1. Support fenced JSON array or object blocks
    for m in _FENCED_JSON_RE.finditer(section):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
        for item in items:
            if isinstance(item, dict):
                prop = _proposal_from_dict(item)
                if prop is not None:
                    proposals.append(prop)
    if proposals:
        return proposals

    # 2. Support structured bullet blocks:
    # - type: handoff
    #   scope: module:src/foo.cpp
    #   tags: REQ-1, foo
    #   summary: ...
    current: dict[str, Any] | None = None
    body_lines: list[str] = []
    in_multiline_body = False

    def _flush() -> None:
        """Finalize accumulated key-value block into ``proposals``."""
        nonlocal current, body_lines, in_multiline_body
        if current is not None:
            if body_lines and not current.get("body") and not current.get("summary"):
                current["body"] = "\n".join(body_lines).strip()
            prop = _proposal_from_dict(current)
            if prop is not None:
                proposals.append(prop)
        current = None
        body_lines = []
        in_multiline_body = False

    for line in section.splitlines():
        s = line.strip()
        if not s:
            if in_multiline_body:
                body_lines.append("")
            continue
        if s.startswith("### "):
            _flush()
            current = {"title": s[4:].strip()}
            continue
        if s.startswith("- ") and ":" in s[2:]:
            k, v = s[2:].split(":", 1)
            key = k.strip().lower()
            if key in ("type", "id", "title") and (current is None or "type" in current):
                _flush()
                current = {}
            elif current is None:
                current = {}
            in_multiline_body = False
            val = v.strip()
            if key in ("body", "summary", "content", "text") and val in ("|", ">", ""):
                in_multiline_body = True
                body_lines = []
            else:
                current[key] = _unquote(val)
            continue
        if current is not None and not in_multiline_body and ":" in s and not s.startswith("- "):
            k, v = s.split(":", 1)
            key = k.strip().lower()
            if key in ("id", "type", "scope", "tags", "supersedes", "expires", "title", "summary", "body", "content", "text"):
                val = v.strip()
                if key in ("body", "summary", "content", "text") and val in ("|", ">", ""):
                    in_multiline_body = True
                    body_lines = []
                else:
                    current[key] = _unquote(val)
                continue
        if current is not None:
            in_multiline_body = True
            body_lines.append(s[2:].strip() if s.startswith("- ") else line.rstrip())

    _flush()
    return proposals


def process_memory_proposals(
    memory_root: Path | str,
    final_text: str,
    *,
    source: str,
    stop_report_text: str = "",
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
) -> ProposalProcessResult:
    """Parse proposals from ``final_text`` and ``stop_report_text`` and store them safely.

    - ``handoff`` and ``known-gap`` proposals are auto-accepted with ``status='active'``, unless
      their ``supersedes`` names a ``decision`` or ``fact`` entry (then stored ``pending``).
    - ``decision`` and ``fact`` proposals are stored with ``status='pending'`` for human approval.
    - A ``supersedes`` id that does not exist rejects the proposal (listed in ``result.rejected``).
    - Any proposal failing schema or secret/PII checks is rejected without writing to disk.
    """
    proposals = parse_memory_proposals(final_text)
    if stop_report_text:
        proposals.extend(parse_memory_proposals(stop_report_text))

    result = ProposalProcessResult()
    root = Path(memory_root)
    for prop in proposals:
        try:
            norm_type = validate_entry_type(prop.entry_type)
            norm_scope = validate_scope(prop.scope)
            supersede_ids = [validate_entry_id(s) for s in prop.supersedes if str(s).strip()]
            existing = {e.id: e for e in load_memory_entries(root)}
            unknown_ids = [sid for sid in supersede_ids if sid not in existing]
            if unknown_ids:
                raise MemoryValidationError(
                    f"Proposal supersedes unknown memory id(s): {', '.join(unknown_ids)}"
                )
            # Superseding a human-approved entry (decision/fact) must wait for human approval,
            # so the supersede is applied only when `approve` writes the entry as active.
            touches_approved = any(existing[sid].entry_type in HUMAN_APPROVAL_TYPES for sid in supersede_ids)
            auto_accept = norm_type in AUTO_ACCEPT_TYPES and not touches_approved
            status = "active" if auto_accept else "pending"
            entry = add_memory_entry(
                root,
                entry_type=norm_type,
                scope=norm_scope,
                body=prop.body,
                tags=prop.tags,
                source=source,
                supersedes=prop.supersedes,
                expires=prop.expires,
                status=status,
                entry_id=prop.id_hint,
                title=prop.title,
                extra_secrets=extra_secrets,
                patterns_file=patterns_file,
            )
            if status == "active":
                result.accepted.append(entry)
            else:
                result.pending.append(entry)
        except MemoryValidationError as exc:
            result.rejected.append(
                {
                    "type": prop.entry_type,
                    "scope": prop.scope,
                    "reason": str(exc),
                }
            )
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    """Build a minimal ``--help`` parser when ``memory.py`` is executed directly."""
    parser = argparse.ArgumentParser(
        prog="memory.py",
        description="Bounded memory library module; use runner/devpack_memory.py for CLI commands.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Display library CLI notice."""
    parser = build_arg_parser()
    parser.parse_args(argv)
    print("Use `python3 runner/devpack_memory.py --help` for memory CLI commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
