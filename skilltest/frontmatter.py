#!/usr/bin/env python3
# frontmatter.py - Shared SKILL.md frontmatter and skilltest.toml parsers.
"""Shared YAML-like frontmatter and TOML config parsing helpers for skilltest."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


def _minimal_toml_loads(text: str) -> dict[str, Any]:
    """Parse flat TOML scalars and arrays of tables used by skilltest on Python 3.10.

    Support quoted strings, booleans, and integers; reject unsupported syntax.
    """
    root: dict[str, Any] = {}
    current = root
    for line_number, line in enumerate(text.splitlines(), 1):
        quote = None
        escaped = False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            elif quote is None and char in ("'", '"'):
                quote = char
            elif quote is None and char == "#":
                line = line[:index]
                break
        line = line.strip()
        if not line:
            continue
        table = re.fullmatch(r"\[\[([A-Za-z_][\w-]*)\]\]", line)
        if table:
            current = {}
            root.setdefault(table[1], []).append(current)
            continue
        entry = re.fullmatch(r"([A-Za-z_][\w-]*)\s*=\s*(.+)", line)
        if not entry:
            raise ValueError(f"unsupported TOML at line {line_number}")
        key, value = entry.groups()
        if key in current:
            raise ValueError(f"duplicate TOML key at line {line_number}")
        if value in ("true", "false"):
            parsed: Any = value == "true"
        elif re.fullmatch(r"[+-]?[0-9]+", value):
            parsed = int(value)
        elif value.startswith('"') and value.endswith('"'):
            parsed = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            parsed = value[1:-1]
        else:
            raise ValueError(f"unsupported TOML value at line {line_number}")
        current[key] = parsed
    return root


def load_config(skill_dir: Path) -> dict[str, Any]:
    """Load a skill test configuration with a Python 3.10 stdlib fallback."""
    config = skill_dir / "tests" / "skilltest.toml"
    if not config.is_file():
        return {}
    text = config.read_text(encoding="utf-8")
    return tomllib.loads(text) if tomllib is not None else _minimal_toml_loads(text)


def parse_frontmatter(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Parse compact YAML-like frontmatter and return metadata, body, and errors."""
    errors: list[str] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines, ["missing frontmatter"]
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return {}, lines, ["unterminated frontmatter"]
    meta: dict[str, str] = {}
    for line in lines[1:closing]:
        if ":" not in line:
            errors.append("invalid frontmatter entry")
            continue
        key, value = line.split(":", 1)
        val = value.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1].strip()
        meta[key.strip()] = val
    return meta, lines[closing + 1 :], errors
