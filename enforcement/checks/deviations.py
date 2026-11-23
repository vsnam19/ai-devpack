#!/usr/bin/env python3
# Header: Human-managed deviation registry verification outside agent-writable worktrees.
# Reads the external deviation registry referenced by DEVPACK_DEVIATIONS (TOML or JSON)
# and verifies entries by ID, file globs, optional rule/check name, approver, and expiry.
#
# Exit codes:
#   Module only (no CLI entrypoint).
"""External human deviation registry loader and verifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import fnmatch
import json
import os
import pathlib
from typing import Any, Sequence

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DeviationEntry:
    """Validated entry from the external human deviation registry."""

    deviation_id: str
    files: tuple[str, ...]
    approved_by: str
    expires: str
    rules: tuple[str, ...] = ()
    reason: str = ""


def _is_within(child: pathlib.Path, parent: pathlib.Path) -> bool:
    """Return True when ``child`` resolves inside or equal to ``parent``."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _parse_minimal_toml_or_json(raw_text: str, path: pathlib.Path) -> Any:
    """Parse JSON or TOML registry content using stdlib parsers."""
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError(f"deviation registry is empty: {path}")
    if path.suffix.lower() == ".json" or stripped.startswith(("{", "[")):
        return json.loads(stripped)
    if tomllib is not None:
        return tomllib.loads(raw_text)
    try:
        from runner.agent_exec import _minimal_toml_loads  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        _minimal_toml_loads = None  # type: ignore[assignment]
    if _minimal_toml_loads is not None:
        return _minimal_toml_loads(raw_text)
    raise ValueError("TOML deviation registry requires Python 3.11+ or JSON format")


def _coerce_str_tuple(raw: Any) -> tuple[str, ...]:
    """Normalize a string or sequence of strings into a stripped tuple."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return tuple(parts)
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    raise ValueError(f"expected string or list, got {type(raw).__name__}")


def _extract_raw_entries(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract ``(deviation_id, raw_mapping)`` pairs from supported JSON/TOML layouts."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    if isinstance(data, list):
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"deviation list item #{idx} must be a mapping")
            dev_id = str(item.get("id") or item.get("ID") or "").strip()
            pairs.append((dev_id, item))
        return pairs

    if not isinstance(data, dict):
        raise ValueError("deviation registry root must be an object/table or list")

    raw_devs = data.get("deviations", data.get("deviation"))
    if isinstance(raw_devs, list):
        return _extract_raw_entries(raw_devs)
    if isinstance(raw_devs, dict):
        for key, val in raw_devs.items():
            if not isinstance(val, dict):
                raise ValueError(f"deviation '{key}' must be a mapping")
            dev_id = str(val.get("id") or val.get("ID") or key).strip()
            pairs.append((dev_id, val))
        return pairs

    for key, val in data.items():
        if isinstance(val, dict):
            dev_id = str(val.get("id") or val.get("ID") or key).strip()
            pairs.append((dev_id, val))
    return pairs


def _parse_expiry(expires_str: str) -> tuple[bool, datetime | date]:
    """Parse an ISO date or datetime expiry string; return ``(is_datetime, value)``."""
    clean = expires_str.strip()
    if not clean:
        raise ValueError("missing expires timestamp")
    if len(clean) == 10 and clean[4] == "-" and clean[7] == "-":
        return False, date.fromisoformat(clean)
    normalized = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return True, dt


def is_expired(expires_str: str, *, now: datetime | None = None) -> bool:
    """Return True when ``expires_str`` is malformed or earlier than ``now`` (UTC)."""
    ref_now = now or datetime.now(timezone.utc)
    try:
        is_dt, val = _parse_expiry(expires_str)
    except ValueError:
        return True
    if not is_dt:
        assert isinstance(val, date)
        return val < ref_now.date()
    assert isinstance(val, datetime)
    return val < ref_now


def resolve_registry_path(registry_path: pathlib.Path | str | None = None) -> pathlib.Path | None:
    """Return the configured human deviation registry path, or None if unset."""
    if registry_path is not None and str(registry_path).strip():
        return pathlib.Path(str(registry_path).strip())
    env_val = os.environ.get("DEVPACK_DEVIATIONS", "").strip()
    if env_val:
        return pathlib.Path(env_val)
    return None


def load_deviations_registry(
    registry_path: pathlib.Path | str | None = None,
    *,
    worktree_roots: Sequence[pathlib.Path | str] = (),
) -> tuple[dict[str, DeviationEntry] | None, str | None]:
    """Load and validate the external human deviation registry.

    Refuses missing files or registries stored inside any agent-writable worktree.
    """
    resolved = resolve_registry_path(registry_path)
    if resolved is None:
        return None, "human deviation registry not configured (set DEVPACK_DEVIATIONS)"

    abs_reg = resolved.resolve()
    for root in worktree_roots:
        if not root:
            continue
        root_path = pathlib.Path(root).resolve()
        if _is_within(abs_reg, root_path):
            return (
                None,
                f"invalid deviation registry: '{abs_reg}' must live outside worktree '{root_path}'",
            )

    if not abs_reg.is_file():
        return None, f"deviation registry not found: {abs_reg}"

    try:
        raw_text = abs_reg.read_text(encoding="utf-8")
        data = _parse_minimal_toml_or_json(raw_text, abs_reg)
        pairs = _extract_raw_entries(data)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"failed to parse deviation registry '{abs_reg}': {exc}"

    entries: dict[str, DeviationEntry] = {}
    for dev_id, raw in pairs:
        if not dev_id:
            return None, f"deviation registry '{abs_reg}' contains an entry without id"
        if dev_id in entries:
            return None, f"duplicate deviation id '{dev_id}' in '{abs_reg}'"
        try:
            files = _coerce_str_tuple(raw.get("files"))
            rules = _coerce_str_tuple(raw.get("rules", raw.get("rule")))
        except ValueError as exc:
            return None, f"deviation '{dev_id}' invalid field: {exc}"

        approved_by = str(raw.get("approved_by") or "").strip()
        if not approved_by:
            return None, f"deviation '{dev_id}' missing non-empty approved_by"

        expires = str(raw.get("expires") or "").strip()
        try:
            _parse_expiry(expires)
        except ValueError:
            return None, f"deviation '{dev_id}' has missing or invalid expires '{expires}'"

        entries[dev_id] = DeviationEntry(
            deviation_id=dev_id,
            files=files,
            approved_by=approved_by,
            expires=expires,
            rules=rules,
            reason=str(raw.get("reason") or "").strip(),
        )
    return entries, None


def _file_matches_patterns(rel_path: str, patterns: Sequence[str]) -> bool:
    """Return True when ``rel_path`` matches any glob or literal file entry in ``patterns``."""
    norm = rel_path.replace("\\", "/").lstrip("./")
    for pat in patterns:
        norm_pat = pat.replace("\\", "/").lstrip("./")
        if not norm_pat:
            continue
        if norm_pat == "*" or norm_pat == "**" or norm == norm_pat or fnmatch.fnmatch(norm, norm_pat):
            return True
    return False


def verify_deviation(
    deviation_id: str,
    *,
    files: Sequence[str] = (),
    rule: str | None = None,
    registry_path: pathlib.Path | str | None = None,
    worktree_roots: Sequence[pathlib.Path | str] = (),
    now: datetime | None = None,
) -> tuple[DeviationEntry | None, str]:
    """Verify ``deviation_id`` against the external human registry for ``files`` and ``rule``."""
    clean_id = (deviation_id or "").strip()
    if not clean_id:
        return None, "missing deviation ID"

    entries, err = load_deviations_registry(registry_path, worktree_roots=worktree_roots)
    if entries is None:
        return None, err or "deviation registry unavailable"

    entry = entries.get(clean_id)
    if entry is None:
        return None, f"deviation '{clean_id}' not found in human registry"

    if is_expired(entry.expires, now=now):
        return None, f"deviation '{clean_id}' expired on {entry.expires}"

    if rule and entry.rules and "*" not in entry.rules and rule not in entry.rules:
        return None, f"deviation '{clean_id}' does not cover rule '{rule}'"

    if files:
        if not entry.files:
            return None, f"deviation '{clean_id}' lists no files (required for matched file override)"
        unmatched = [f for f in files if not _file_matches_patterns(f, entry.files)]
        if unmatched:
            return (
                None,
                f"deviation '{clean_id}' does not cover file(s): {', '.join(unmatched)}",
            )

    return entry, ""
