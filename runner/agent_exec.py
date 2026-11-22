#!/usr/bin/env python3
# agent_exec.py - Single isolated agent executor, v2 engine loader, and stream-json parser.
#
# Usage:
#   python3 runner/agent_exec.py [--help] [--check-config PATH] [--parse-stream PATH]
#
# Launches cloud (Sonnet/Haiku) or local (Anthropic-compatible endpoint) agents in
# local or Docker isolation, streams redacted stream-json output into the shared
# run-dir layout (run.json, steps/NN-<role>-<engine>.{jsonl,meta.json,prompt.md}),
# cleans up credentials copies, and classifies status (ok, agent_error, timeout,
# harness_error).
#
# Exit codes:
#   0 - Requested inspection or execution completed successfully
#   1 - Runtime, stream parsing, or agent execution failure
#   2 - Invalid command-line arguments or malformed configuration
"""Unified agent executor, v2/v0.1 engine configuration loader, and stream-json parser."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping, Sequence
import uuid

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

DEFAULT_CLAUDE_VERSION = "2.1.330"
DOCKER_IMAGE_TAG = f"devpack-eval:{DEFAULT_CLAUDE_VERSION}"
DOCKER_TASK_DIR = "/workspace/task"
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
DEFAULT_GATE_CMD: tuple[str, ...] = (
    "python3",
    "enforcement/gate/gate.py",
    "--config",
    "enforcement/gate/gate.example.toml",
)
VALID_ENGINE_CLASSES = ("cloud", "local")
VALID_RUN_KINDS = ("task", "eval", "dispatch", "skilltest")
DATA_CLASS_ORDER: tuple[str, ...] = (
    "public",
    "internal",
    "confidential",
    "customer-restricted",
    "secret",
)
LOCAL_ISOLATION_WARNING = (
    "WARNING: running with isolation='local' (unsandboxed host filesystem and "
    "network access; sandbox deviation per policy/sandbox.md)."
)

# Assemble host-home regex from fragments so forbidden literal substrings never appear.
_HOME_PREFIX = "/" + "ho" + "me/"
_HOST_HOME_RE = re.compile(
    r"(?:" + re.escape(_HOME_PREFIX) + r"[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+)"
)

_STEP_PREFIX_RE = re.compile(r"^\d{2,}-.+")
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_INLINE_JSON_TOOL_RE = re.compile(
    r"\{\s*\"(?:name|tool|tool_name|function)\"\s*:\s*\"[^\"]+\"\s*,"
    r"\s*\"(?:arguments|input|parameters|args|params)\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)
_SECRET_ENV_SUFFIXES = ("_AUTH_TOKEN", "_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
_AUTH_FAILURE_PATTERNS = (
    "authentication_error",
    "authentication failed",
    "invalid api key",
    "invalid_api_key",
    "invalid x-api-key",
    "401 unauthorized",
    "http 401",
    "status 401",
    "not logged in",
    "please run /login",
    "expired credential",
    "oauth token has expired",
    "invalid bearer token",
)


class HarnessError(RuntimeError):
    """Raised when the execution harness or environment configuration cannot launch the agent."""


def _get_secret_pii_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Load built-in and optional external regex rules from secret_pii_check."""
    begin_word = "-----BEGIN"
    priv_key_word = "PRIVATE" + " KEY-----"
    pwd_word = "pass" + "word"
    passwd_word = "pass" + "wd"
    aws_sec_word = "aws_secret" + "_access_key"
    rules: list[tuple[str, re.Pattern[str]]] = [
        (
            "private-key-header",
            re.compile(rf"{begin_word}\s+(?:[A-Z0-9 ]+\s+)?{priv_key_word}"),
        ),
        (
            "aws-access-key-id",
            re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        ),
        (
            "aws-secret-key",
            re.compile(rf"(?i)\b{aws_sec_word}\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{{20,}}"),
        ),
        (
            "generic-password",
            re.compile(
                rf"(?i)\b(?:{pwd_word}|{passwd_word})\s*=\s*(?!['\"]?\s*(?:$|#))['\"]?[^\s'\";#]+"
            ),
        ),
    ]
    ext_path_str = os.environ.get("DEVPACK_SECRET_PATTERNS", "").strip()
    if ext_path_str:
        ext_path = Path(ext_path_str)
        if ext_path.is_file():
            try:
                for idx, raw_line in enumerate(
                    ext_path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    try:
                        rules.append((f"custom-pattern-{idx}", re.compile(stripped, re.IGNORECASE)))
                    except re.error:
                        continue
            except OSError:
                pass
    return rules


@dataclass
class EngineConfig:
    """Configuration for a single cloud or local agent execution engine."""

    name: str = "sonnet"
    model: str = "sonnet"
    engine_class: str = "cloud"
    max_data_class: str = "internal"
    base_url_env: str = ""
    auth_token_env: str = ""
    min_context_tokens: int = 0
    timeout_s: int = 900
    cmd: list[str] | None = None

    def __init__(
        self,
        name: str = "",
        model: str = "sonnet",
        engine_class: str = "cloud",
        max_data_class: str = "internal",
        base_url_env: str = "",
        auth_token_env: str = "",
        min_context_tokens: int = 0,
        timeout_s: int = 900,
        cmd: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize an EngineConfig, accepting ``class`` or ``kind`` as alias for ``engine_class``."""
        if "class" in kwargs:
            engine_class = str(kwargs.pop("class"))
        if "kind" in kwargs:
            engine_class = str(kwargs.pop("kind"))
        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(f"Unexpected keyword argument(s) for EngineConfig: {unexpected}")
        resolved_name = name or model or "sonnet"
        self.name = str(resolved_name)
        self.model = str(model or resolved_name)
        self.engine_class = str(engine_class or "cloud")
        self.max_data_class = str(max_data_class or "internal")
        self.base_url_env = str(base_url_env or "")
        self.auth_token_env = str(auth_token_env or "")
        self.min_context_tokens = int(min_context_tokens or 0)
        self.timeout_s = int(timeout_s or 900)
        self.cmd = list(cmd) if cmd is not None else None

    @property
    def is_local(self) -> bool:
        """Return True when this engine points at a local Anthropic-compatible server."""
        return self.engine_class == "local"

    @property
    def is_cloud(self) -> bool:
        """Return True when this engine uses a first-party cloud model."""
        return self.engine_class == "cloud"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the engine configuration into a plain dictionary."""
        data = asdict(self)
        data["class"] = self.engine_class
        return data

    def __getitem__(self, key: str) -> Any:
        """Support dictionary-style lookup on EngineConfig (including ``['class']``)."""
        if key == "class":
            return self.engine_class
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return an attribute or ``'class'`` value by key, or ``default`` if absent."""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        """Return True if ``key`` is a valid field name or ``'class'``."""
        if not isinstance(key, str):
            return False
        return key == "class" or hasattr(self, key)


setattr(EngineConfig, "class", property(lambda self: self.engine_class))


@dataclass
class EnginesConfig:
    """Loaded engines configuration containing engines, role mappings, and limits."""

    version: int = 2
    engines: dict[str, EngineConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=lambda: {"max_fix_rounds": 2})
    max_fix_rounds: int = 2
    gate_cmd: list[str] = field(default_factory=lambda: list(DEFAULT_GATE_CMD))
    gate_timeout_s: int = 300
    role_engines: dict[str, EngineConfig] = field(default_factory=dict)

    def get_engine(self, name_or_role: str) -> EngineConfig:
        """Resolve an engine by engine name or by role name."""
        if name_or_role in self.engines:
            return self.engines[name_or_role]
        if name_or_role in self.role_engines:
            return self.role_engines[name_or_role]
        if name_or_role in self.roles:
            target = self.roles[name_or_role]
            if target in self.engines:
                return self.engines[target]
        raise KeyError(f"Unknown engine or role: {name_or_role}")

    def engine_for_role(self, role: str) -> EngineConfig:
        """Return the EngineConfig assigned to ``role``."""
        if role in self.role_engines:
            return self.role_engines[role]
        if role in self.roles:
            target = self.roles[role]
            if target in self.engines:
                return self.engines[target]
        if role in self.engines:
            return self.engines[role]
        raise KeyError(f"Role '{role}' is not defined in configuration")

    def __getitem__(self, key: str) -> Any:
        """Support indexing by top-level section name or directly by engine/role name."""
        if key in ("version", "engines", "roles", "limits", "max_fix_rounds", "gate_cmd", "gate_timeout_s"):
            return getattr(self, key)
        if key in self.engines:
            return self.engines[key]
        if key in self.role_engines:
            return self.role_engines[key]
        if key in self.roles and self.roles[key] in self.engines:
            return self.engines[self.roles[key]]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a section or engine by key, or ``default`` when not found."""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        """Return True if ``key`` is an engine name, role name, or top-level section."""
        if not isinstance(key, str):
            return False
        return (
            key in self.engines
            or key in self.roles
            or key in self.role_engines
            or key in ("version", "engines", "roles", "limits", "max_fix_rounds", "gate_cmd", "gate_timeout_s")
        )

    def __iter__(self) -> Iterator[str]:
        """Iterate over configured engine names."""
        return iter(self.engines)

    def __len__(self) -> int:
        """Return the number of configured engines."""
        return len(self.engines)

    def keys(self) -> Any:
        """Return the view of configured engine names."""
        return self.engines.keys()

    def values(self) -> Any:
        """Return the view of configured EngineConfig objects."""
        return self.engines.values()

    def items(self) -> Any:
        """Return ``(name, EngineConfig)`` pairs."""
        return self.engines.items()


def _minimal_toml_loads(text: str) -> dict[str, Any]:
    """Parse a minimal subset of TOML (key=value, arrays, and [section.subsection]) for Python 3.10."""
    root: dict[str, Any] = {}
    current = root
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        raw = lines[idx].strip()
        idx += 1
        if not raw or raw.startswith("#"):
            continue
        if "#" in raw:
            in_quote: str | None = None
            for pos, ch in enumerate(raw):
                if ch in ('"', "'") and (pos == 0 or raw[pos - 1] != "\\"):
                    if in_quote == ch:
                        in_quote = None
                    elif in_quote is None:
                        in_quote = ch
                elif ch == "#" and in_quote is None:
                    raw = raw[:pos].strip()
                    break
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            section_path = raw[1:-1].strip().split(".")
            current = root
            for part in section_path:
                current = current.setdefault(part, {})
            continue
        if "=" not in raw:
            continue
        key, val_str = raw.split("=", 1)
        key = key.strip()
        val_str = val_str.strip()
        if val_str.startswith("[") and not val_str.endswith("]"):
            buf = [val_str]
            while idx < len(lines):
                nxt = lines[idx].strip()
                idx += 1
                if not nxt or nxt.startswith("#"):
                    continue
                buf.append(nxt)
                if nxt.endswith("]"):
                    break
            val_str = " ".join(buf)
        if val_str.startswith("[") and val_str.endswith("]"):
            inner = val_str[1:-1].strip()
            if not inner:
                current[key] = []
            else:
                items = [
                    item.strip().strip('"').strip("'")
                    for item in inner.split(",")
                    if item.strip()
                ]
                current[key] = items
        elif val_str.startswith(('"', "'")) and val_str.endswith(('"', "'")):
            current[key] = val_str[1:-1]
        elif val_str.lower() in ("true", "false"):
            current[key] = val_str.lower() == "true"
        else:
            try:
                current[key] = int(val_str)
            except ValueError:
                current[key] = val_str
    return root


def load_engines(path: Path | str) -> EnginesConfig:
    """Load an engines configuration file in v2 format or v0.1 legacy format.

    When loading a v0.1 ``[roles.<name>] cmd = [...]`` file, emits a
    ``DeprecationWarning`` notice to ``sys.stderr`` and converts each role definition
    into an ``EngineConfig``.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"Engines config file not found: {config_path}")
    text = config_path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"Failed to parse TOML in {config_path}: {exc}") from exc
    else:  # pragma: no cover
        data = _minimal_toml_loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid TOML configuration in {config_path}")

    raw_limits = data.get("limits", {})
    if not isinstance(raw_limits, dict):
        raw_limits = {}
    max_fix_rounds = int(raw_limits.get("max_fix_rounds", data.get("max_fix_rounds", 2)))
    limits = dict(raw_limits)
    limits["max_fix_rounds"] = max_fix_rounds

    raw_gate_cmd = data.get("gate_cmd", raw_limits.get("gate_cmd", list(DEFAULT_GATE_CMD)))
    if isinstance(raw_gate_cmd, str):
        gate_cmd = shlex.split(raw_gate_cmd)
    elif isinstance(raw_gate_cmd, list):
        gate_cmd = [str(item) for item in raw_gate_cmd]
    else:
        gate_cmd = list(DEFAULT_GATE_CMD)
    gate_timeout_s = int(data.get("gate_timeout_s", raw_limits.get("gate_timeout_s", 300)))

    # Detect v2 format: version == 2 or [engines] table present
    if data.get("version") == 2 or "engines" in data:
        raw_engines = data.get("engines")
        if not isinstance(raw_engines, dict) or not raw_engines:
            raise ValueError(f"Engines v2 config must define a non-empty [engines] table: {config_path}")

        engines: dict[str, EngineConfig] = {}
        for eng_name, eng_spec in raw_engines.items():
            if not isinstance(eng_spec, dict):
                raise ValueError(f"Engine '{eng_name}' must be a TOML table")
            model = str(eng_spec.get("model", "")).strip()
            if not model:
                raise ValueError(f"Engine '{eng_name}' is missing required 'model' field")
            eng_class = str(eng_spec.get("class", eng_spec.get("engine_class", "cloud"))).strip()
            if eng_class not in VALID_ENGINE_CLASSES:
                raise ValueError(
                    f"Engine '{eng_name}' has invalid class '{eng_class}' (must be cloud or local)"
                )
            max_data_class = str(eng_spec.get("max_data_class", "internal")).strip()
            base_url_env = str(eng_spec.get("base_url_env", "")).strip()
            auth_token_env = str(eng_spec.get("auth_token_env", "")).strip()
            if eng_class == "local":
                if base_url_env.startswith(("http://", "https://")):
                    raise ValueError(
                        f"Engine '{eng_name}' base_url_env must be an environment variable NAME, not a URL"
                    )
            min_ctx = int(eng_spec.get("min_context_tokens", 0))
            timeout_s = int(eng_spec.get("timeout_s", 900))
            raw_cmd = eng_spec.get("cmd")
            cmd_list = [str(x) for x in raw_cmd] if isinstance(raw_cmd, list) else None
            engines[eng_name] = EngineConfig(
                name=eng_name,
                model=model,
                engine_class=eng_class,
                max_data_class=max_data_class,
                base_url_env=base_url_env,
                auth_token_env=auth_token_env,
                min_context_tokens=min_ctx,
                timeout_s=timeout_s,
                cmd=cmd_list,
            )

        raw_roles = data.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise ValueError("[roles] must be a table mapping role names to engine names")
        roles: dict[str, str] = {}
        role_engines: dict[str, EngineConfig] = {}
        for role_name, target in raw_roles.items():
            if isinstance(target, str):
                if target not in engines:
                    raise ValueError(
                        f"Role '{role_name}' references undefined engine '{target}'"
                    )
                roles[role_name] = target
                role_engines[role_name] = engines[target]
            elif isinstance(target, dict):
                # Mixed v0.1 role dict inside a config
                sys.stderr.write(
                    f"DeprecationWarning: {config_path} defines [roles.{role_name}] as a table; "
                    "use engines v2 role = \"<engine>\" mapping instead.\n"
                )
                model = str(target.get("model", "sonnet"))
                cmd_raw = target.get("cmd")
                cmd_list = [str(x) for x in cmd_raw] if isinstance(cmd_raw, list) else None
                ec = EngineConfig(
                    name=model,
                    model=model,
                    engine_class="cloud",
                    timeout_s=int(target.get("timeout_s", 900)),
                    cmd=cmd_list,
                )
                role_engines[role_name] = ec
                engines.setdefault(model, ec)
                roles[role_name] = model
            else:
                raise ValueError(f"Invalid role specification for '{role_name}'")

        return EnginesConfig(
            version=int(data.get("version", 2)),
            engines=engines,
            roles=roles,
            limits=limits,
            max_fix_rounds=max_fix_rounds,
            gate_cmd=gate_cmd,
            gate_timeout_s=gate_timeout_s,
            role_engines=role_engines,
        )

    # Check v0.1 format: [roles.<name>] tables with cmd = [...]
    raw_roles = data.get("roles")
    if isinstance(raw_roles, dict) and raw_roles:
        sys.stderr.write(
            f"DeprecationWarning: {config_path} uses v0.1 [roles.<role>] cmd=[...] format; "
            "please migrate to engines.toml v2 (version = 2).\n"
        )
        engines = {}
        roles = {}
        role_engines = {}
        for role_name, role_spec in raw_roles.items():
            if not isinstance(role_spec, dict):
                raise ValueError(f"Invalid v0.1 role definition for '{role_name}'")
            model = str(role_spec.get("model", "sonnet")).strip() or "sonnet"
            raw_cmd = role_spec.get("cmd")
            if isinstance(raw_cmd, str):
                cmd_list: list[str] | None = shlex.split(raw_cmd)
            elif isinstance(raw_cmd, list):
                cmd_list = [str(x) for x in raw_cmd]
            else:
                cmd_list = None
            timeout_s = int(role_spec.get("timeout_s", 900))
            role_ec = EngineConfig(
                name=model,
                model=model,
                engine_class="cloud",
                max_data_class="internal",
                timeout_s=timeout_s,
                cmd=cmd_list,
            )
            role_engines[role_name] = role_ec
            engines[role_name] = EngineConfig(
                name=role_name,
                model=model,
                engine_class="cloud",
                max_data_class="internal",
                timeout_s=timeout_s,
                cmd=cmd_list,
            )
            engines.setdefault(model, role_ec)
            roles[role_name] = model

        return EnginesConfig(
            version=1,
            engines=engines,
            roles=roles,
            limits=limits,
            max_fix_rounds=max_fix_rounds,
            gate_cmd=gate_cmd,
            gate_timeout_s=gate_timeout_s,
            role_engines=role_engines,
        )

    raise ValueError(f"Configuration in {config_path} defines neither [engines] (v2) nor [roles] (v0.1)")


def default_engine_config(name: str) -> EngineConfig:
    """Return a built-in default EngineConfig for ``sonnet``, ``haiku``, or ``local``."""
    cleaned = name.strip()
    if cleaned == "local":
        return EngineConfig(
            name="local",
            model="local-coder",
            engine_class="local",
            max_data_class="confidential",
            base_url_env="DEVPACK_LOCAL_BASE_URL",
            auth_token_env="DEVPACK_LOCAL_AUTH_TOKEN",
            min_context_tokens=32768,
            timeout_s=900,
        )
    return EngineConfig(
        name=cleaned,
        model=cleaned,
        engine_class="cloud",
        max_data_class="internal",
        timeout_s=900,
    )


def resolve_engine(
    name: str,
    config: EnginesConfig | Path | str | None = None,
) -> EngineConfig:
    """Resolve ``name`` from ``config`` if provided, else from ``runner/engines.example.toml`` or defaults."""
    loaded: EnginesConfig | None = None
    if isinstance(config, EnginesConfig):
        loaded = config
    elif isinstance(config, (Path, str)) and str(config):
        cfg_path = Path(config)
        if cfg_path.is_file():
            loaded = load_engines(cfg_path)
    else:
        default_path = Path(__file__).resolve().parent / "engines.example.toml"
        if default_path.is_file():
            try:
                loaded = load_engines(default_path)
            except ValueError:
                loaded = None

    if loaded is not None:
        try:
            return loaded.get_engine(name)
        except KeyError:
            pass
    return default_engine_config(name)


def load_project_config(
    workdir: Path | str | None = None,
    project_config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load ``.devpack/project.toml`` from ``workdir`` (missing file defaults to ``internal``)."""
    defaults: dict[str, Any] = {
        "data_class": "internal",
        "engines_config": "runner/engines.example.toml",
        "runs_root": "runs",
        "default_engines": {},
        "allow_unsandboxed_local": False,
        "config_found": False,
    }
    if project_config_path is not None and str(project_config_path):
        cfg_path = Path(project_config_path)
    elif workdir is not None:
        cfg_path = Path(workdir) / ".devpack" / "project.toml"
    else:
        return defaults

    if not cfg_path.is_file():
        return defaults

    text = cfg_path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            raw = tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"Failed to parse TOML in {cfg_path}: {exc}") from exc
    else:  # pragma: no cover
        raw = _minimal_toml_loads(text)

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid TOML project configuration in {cfg_path}")

    data_class = str(raw.get("data_class", "internal")).strip().lower()
    if data_class not in DATA_CLASS_ORDER:
        raise ValueError(
            f"Invalid data_class '{data_class}' in {cfg_path} "
            f"(must be one of {', '.join(DATA_CLASS_ORDER)})"
        )
    defaults["data_class"] = data_class
    defaults["engines_config"] = str(raw.get("engines_config", defaults["engines_config"])).strip()
    defaults["runs_root"] = str(raw.get("runs_root", defaults["runs_root"])).strip()
    raw_defaults = raw.get("default_engines", {})
    if isinstance(raw_defaults, dict):
        defaults["default_engines"] = {str(k): str(v) for k, v in raw_defaults.items()}
    defaults["allow_unsandboxed_local"] = bool(raw.get("allow_unsandboxed_local", False))
    defaults["config_found"] = True
    return defaults


def check_data_class_allowed(engine: EngineConfig, data_class: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` checking ``engine.max_data_class`` against ``data_class``."""
    proj_cls = data_class.strip().lower() or "internal"
    eng_cls = engine.max_data_class.strip().lower() or "internal"
    if proj_cls not in DATA_CLASS_ORDER:
        return False, f"Unknown project data_class '{proj_cls}' (fail closed)."
    if eng_cls not in DATA_CLASS_ORDER:
        return (
            False,
            f"Engine '{engine.name}' has unknown max_data_class '{eng_cls}' (fail closed).",
        )
    if proj_cls == "secret":
        return (
            False,
            f"Refused: project data_class is 'secret'; no AI engine ('{engine.name}') may process secret data (policy/data_governance.md).",
        )
    if DATA_CLASS_ORDER.index(proj_cls) > DATA_CLASS_ORDER.index(eng_cls):
        return (
            False,
            f"Refused: engine '{engine.name}' (max_data_class='{eng_cls}') is not permitted for project data_class='{proj_cls}' (policy/data_governance.md).",
        )
    return True, ""


def check_local_isolation_allowed(
    isolation: str,
    *,
    allow_unsandboxed_local: bool = False,
    workdir: Path | str | None = None,
    project_config: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Enforce fail-safe sandbox policy for ``isolation='local'`` (refuse unless explicitly allowed)."""
    if isolation == "docker":
        return True, ""
    if isolation != "local":
        raise ValueError(f"Unsupported isolation mode: {isolation}")

    cfg = dict(project_config) if project_config is not None else {}
    if not cfg and workdir is not None:
        try:
            cfg = load_project_config(workdir)
        except ValueError:
            cfg = {}

    if allow_unsandboxed_local or bool(cfg.get("allow_unsandboxed_local", False)):
        return True, LOCAL_ISOLATION_WARNING

    return (
        False,
        "Refused: isolation='local' runs unsandboxed on the host filesystem and network "
        "(policy/sandbox.md). Use --isolation docker (default), or pass "
        "--allow-unsandboxed-local / set allow_unsandboxed_local = true in .devpack/project.toml.",
    )


def load_quality_checklist(repo_root: Path | str | None = None) -> str:
    """Read ``runner/quality_checklist.md`` from disk at call time (empty string if absent)."""
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(Path(repo_root) / "runner" / "quality_checklist.md")
    candidates.append(Path(__file__).resolve().parent / "quality_checklist.md")
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""


@dataclass
class AgentRequest:
    """Input contract for launching a single isolated agent via ``run_agent``."""

    engine: EngineConfig
    prompt: str
    workdir: Path
    run_dir: Path
    step_name: str = ""
    isolation: str = "docker"
    allow_unsandboxed_local: bool = False
    allowed_tools: Sequence[str] = ALLOWED_TOOLS
    disallowed_tools: Sequence[str] = DISALLOWED_TOOLS
    timeout_s: int | None = None
    skills_enabled: bool = False
    append_system_prompt: str | None = None
    credentials_path: Path | None = None
    agent_bin: str = "claude"
    docker_bin: str = "docker"
    image_tag: str = DOCKER_IMAGE_TAG
    extra_secrets: Sequence[str] = ()
    path_replacements: Sequence[tuple[str, str]] = ()
    role: str = "agent"
    step_index: int = 1
    home_dir: Path | None = None
    cfg_dir: Path | None = None
    tmp_dir: Path | None = None
    env_overrides: dict[str, str] | None = None
    home_placeholder: str = "~"

    def __init__(
        self,
        engine: EngineConfig | None = None,
        prompt: str = "",
        workdir: Path | str = ".",
        run_dir: Path | str | None = None,
        step_name: str = "",
        isolation: str = "docker",
        allow_unsandboxed_local: bool = False,
        allowed_tools: Sequence[str] = ALLOWED_TOOLS,
        disallowed_tools: Sequence[str] = DISALLOWED_TOOLS,
        timeout_s: int | None = None,
        skills_enabled: bool = False,
        append_system_prompt: str | None = None,
        credentials_path: Path | str | None = None,
        agent_bin: str = "claude",
        docker_bin: str = "docker",
        image_tag: str = DOCKER_IMAGE_TAG,
        extra_secrets: Sequence[str] = (),
        path_replacements: Sequence[tuple[str, str]] = (),
        role: str = "agent",
        step_index: int = 1,
        home_dir: Path | str | None = None,
        cfg_dir: Path | str | None = None,
        tmp_dir: Path | str | None = None,
        env_overrides: dict[str, str] | None = None,
        home_placeholder: str = "~",
        **kwargs: Any,
    ) -> None:
        """Initialize AgentRequest with support for SPEC parameter aliases (timeout, engine_config, prompt_text)."""
        if "engine_config" in kwargs and engine is None:
            engine = kwargs.pop("engine_config")
        if "prompt_text" in kwargs and not prompt:
            prompt = str(kwargs.pop("prompt_text"))
        if "timeout" in kwargs and timeout_s is None:
            timeout_s = int(kwargs.pop("timeout"))
        if "docker_image" in kwargs:
            image_tag = str(kwargs.pop("docker_image"))
        if kwargs:
            unexpected = ", ".join(sorted(kwargs.keys()))
            raise TypeError(f"Unexpected keyword argument(s) for AgentRequest: {unexpected}")

        if engine is None:
            engine = default_engine_config("sonnet")
        elif isinstance(engine, str):
            engine = resolve_engine(engine)

        self.engine = engine
        self.prompt = str(prompt)
        self.workdir = Path(workdir)
        self.run_dir = Path(run_dir) if run_dir is not None else (self.workdir / "runs" / "default")
        self.step_name = str(step_name)
        self.isolation = str(isolation or "docker")
        self.allow_unsandboxed_local = bool(allow_unsandboxed_local)
        self.allowed_tools = tuple(allowed_tools) if allowed_tools is not None else ALLOWED_TOOLS
        self.disallowed_tools = (
            tuple(disallowed_tools) if disallowed_tools is not None else DISALLOWED_TOOLS
        )
        self.timeout_s = int(timeout_s) if timeout_s is not None else int(self.engine.timeout_s)
        self.skills_enabled = bool(skills_enabled)
        self.append_system_prompt = append_system_prompt
        self.credentials_path = (
            Path(credentials_path) if credentials_path is not None and str(credentials_path) else None
        )
        self.agent_bin = str(agent_bin or "claude")
        self.docker_bin = str(docker_bin or "docker")
        self.image_tag = str(image_tag or DOCKER_IMAGE_TAG)
        self.extra_secrets = tuple(extra_secrets)
        self.path_replacements = tuple(path_replacements)
        self.role = str(role or "agent")
        self.step_index = int(step_index)
        self.home_dir = Path(home_dir) if home_dir is not None else None
        self.cfg_dir = Path(cfg_dir) if cfg_dir is not None else None
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else None
        self.env_overrides = dict(env_overrides) if env_overrides is not None else None
        self.home_placeholder = str(home_placeholder or "~")

    @property
    def timeout(self) -> int:
        """Alias for ``timeout_s``."""
        return self.timeout_s if self.timeout_s is not None else self.engine.timeout_s

    @property
    def prompt_text(self) -> str:
        """Alias for ``prompt``."""
        return self.prompt

    @property
    def engine_config(self) -> EngineConfig:
        """Alias for ``engine``."""
        return self.engine


@dataclass
class AgentResult:
    """Structured result and telemetry returned by ``run_agent`` and ``parse_stream``."""

    status: str = "ok"
    exit_code: int = 0
    final_text: str = ""
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_s: float = 0.0
    num_turns: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    transcript_path: Path = field(default_factory=Path)
    tool_protocol_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    stderr: str = ""
    permission_denials: list[Any] = field(default_factory=list)
    model: str = ""
    engine: str = ""
    role: str = ""
    prompt_sha256: str = ""
    prompt_file: str = ""

    def __post_init__(self) -> None:
        """Synchronize token field aliases and coerce transcript_path to Path."""
        if self.tokens_in and not self.input_tokens:
            self.input_tokens = self.tokens_in
        elif self.input_tokens and not self.tokens_in:
            self.tokens_in = self.input_tokens
        if self.tokens_out and not self.output_tokens:
            self.output_tokens = self.tokens_out
        elif self.output_tokens and not self.tokens_out:
            self.tokens_out = self.output_tokens
        if isinstance(self.transcript_path, str):
            self.transcript_path = Path(self.transcript_path) if self.transcript_path else Path()

    @property
    def returncode(self) -> int:
        """Alias for ``exit_code``."""
        return self.exit_code

    @property
    def turns(self) -> int:
        """Alias for ``num_turns``."""
        return self.num_turns

    @property
    def result(self) -> str:
        """Alias for ``final_text``."""
        return self.final_text

    @property
    def harness_error(self) -> bool:
        """Return True when the run terminated due to a harness error or timeout."""
        return self.status in ("harness_error", "timeout")

    @property
    def harness_error_reason(self) -> str:
        """Return the error explanation when ``harness_error`` is True."""
        return self.error if self.harness_error else ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary with stringified paths."""
        payload = asdict(self)
        payload["transcript_path"] = str(self.transcript_path) if str(self.transcript_path) != "." else ""
        return payload

    def keys(self) -> Any:
        """Return dictionary keys for mapping unpacking compatibility."""
        return self.to_dict().keys()

    def values(self) -> Any:
        """Return dictionary values for mapping compatibility."""
        return self.to_dict().values()

    def items(self) -> Any:
        """Return ``(key, value)`` pairs for mapping compatibility."""
        return self.to_dict().items()

    def __getitem__(self, key: str) -> Any:
        """Support dictionary-style field lookup on ``AgentResult``."""
        if key == "returncode":
            return self.exit_code
        if key == "turns":
            return self.num_turns
        if key == "result":
            return self.final_text
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return field ``key`` or ``default`` when absent."""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        """Return True when ``key`` is an attribute or alias on ``AgentResult``."""
        if not isinstance(key, str):
            return False
        return hasattr(self, key) or key in ("returncode", "turns", "result")

    def __iter__(self) -> Iterator[str]:
        """Iterate over field names."""
        return iter(self.to_dict())

    def __len__(self) -> int:
        """Return the number of serialized fields."""
        return len(self.to_dict())


def build_permission_args(
    allowed_tools: Sequence[str] = ALLOWED_TOOLS,
    disallowed_tools: Sequence[str] = DISALLOWED_TOOLS,
) -> list[str]:
    """Return command-line permission flags that make workspace trust irrelevant."""
    args: list[str] = []
    if allowed_tools:
        args.extend(["--allowedTools", *allowed_tools])
    if disallowed_tools:
        args.extend(["--disallowedTools", *disallowed_tools])
    return args


def build_claude_state(agent_cwd: str, include_onboarding: bool = False) -> dict[str, Any]:
    """Return ``.claude.json`` content marking ``agent_cwd`` as a trusted project."""
    state: dict[str, Any] = {"projects": {agent_cwd: {"hasTrustDialogAccepted": True}}}
    if include_onboarding:
        state["hasCompletedOnboarding"] = True
    return state


def build_project_settings() -> dict[str, Any]:
    """Return the Claude project settings JSON enforcing tool permissions in cwd."""
    return {
        "permissions": {
            "allow": [
                "Read",
                "Edit",
                "Write",
                "Bash",
            ],
            "deny": [
                "WebFetch",
                "WebSearch",
            ],
        }
    }


def format_command(
    cmd_template: str | Sequence[str],
    prompt: str,
    prompt_file: Path | None = None,
    role_model: str = "",
) -> list[str]:
    """Expand ``{prompt}``, ``{prompt_file}``, and ``{model}`` in a v0.1 command template."""
    if isinstance(cmd_template, str):
        tokens = shlex.split(cmd_template)
    else:
        tokens = [str(item) for item in cmd_template]

    if not tokens:
        raise ValueError("Empty command template")

    prompt_file_str = str(prompt_file) if prompt_file is not None else ""
    has_prompt_placeholder = any(
        "{prompt}" in tok or "{prompt_file}" in tok for tok in tokens
    )
    expanded: list[str] = []
    for tok in tokens:
        replaced = (
            tok.replace("{prompt}", prompt)
            .replace("{prompt_file}", prompt_file_str)
            .replace("{model}", role_model)
        )
        expanded.append(replaced)

    if not has_prompt_placeholder:
        expanded.append(prompt)
    return expanded


def _resolve_local_engine_env(
    engine: EngineConfig,
    source_env: Mapping[str, str],
) -> dict[str, str]:
    """Resolve and validate local engine environment variables from variable names in config."""
    if not engine.base_url_env:
        raise HarnessError(
            f"Local engine '{engine.name}' is missing 'base_url_env' in configuration"
        )
    if not engine.auth_token_env:
        raise HarnessError(
            f"Local engine '{engine.name}' is missing 'auth_token_env' in configuration"
        )
    base_url = source_env.get(engine.base_url_env, "").strip()
    if not base_url:
        raise HarnessError(
            f"Missing required environment variable '{engine.base_url_env}' for local engine '{engine.name}'"
        )
    auth_token = source_env.get(engine.auth_token_env, "").strip()
    if not auth_token:
        raise HarnessError(
            f"Missing required environment variable '{engine.auth_token_env}' for local engine '{engine.name}'"
        )
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "ANTHROPIC_MODEL": engine.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": engine.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": engine.model,
    }


def build_invocation(
    request_or_engine: AgentRequest | EngineConfig,
    prompt: str = "",
    workdir: Path | str | None = None,
    *,
    isolation: str = "local",
    home_dir: Path | str | None = None,
    cfg_dir: Path | str | None = None,
    tmp_dir: Path | str | None = None,
    paths: Mapping[str, Path] | None = None,
    allowed_tools: Sequence[str] = ALLOWED_TOOLS,
    disallowed_tools: Sequence[str] = DISALLOWED_TOOLS,
    skills_enabled: bool = False,
    append_system_prompt: str | None = None,
    agent_bin: str = "claude",
    docker_bin: str = "docker",
    image_tag: str = DOCKER_IMAGE_TAG,
    agent_bin_in_container: str = "claude",
    env_overrides: Mapping[str, str] | None = None,
    output_format: str = "stream-json",
    prompt_file: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Construct ``(argv, env)`` for launching an agent in ``local`` or ``docker`` isolation.

    For local engines (``engine.engine_class == 'local'``), looks up ``engine.base_url_env``
    and ``engine.auth_token_env`` in the environment, sets ``ANTHROPIC_BASE_URL``,
    ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_MODEL``, ``ANTHROPIC_DEFAULT_HAIKU_MODEL``,
    and ``ANTHROPIC_DEFAULT_SONNET_MODEL``, and ensures ``ANTHROPIC_API_KEY`` is removed.
    Raises ``HarnessError`` if required local engine environment variables are unset.
    """
    if isinstance(request_or_engine, AgentRequest):
        req = request_or_engine
        engine = req.engine
        prompt = req.prompt
        resolved_workdir = req.workdir
        isolation = req.isolation
        allowed_tools = req.allowed_tools
        disallowed_tools = req.disallowed_tools
        skills_enabled = req.skills_enabled
        append_system_prompt = req.append_system_prompt
        agent_bin = req.agent_bin
        docker_bin = req.docker_bin
        image_tag = req.image_tag
        if home_dir is None:
            home_dir = req.home_dir
        if cfg_dir is None:
            cfg_dir = req.cfg_dir
        if tmp_dir is None:
            tmp_dir = req.tmp_dir
        if env_overrides is None:
            env_overrides = req.env_overrides
    else:
        engine = request_or_engine
        resolved_workdir = Path(workdir) if workdir is not None else Path.cwd()

    if paths is not None:
        resolved_workdir = Path(paths.get("work_dir", resolved_workdir))
        if home_dir is None and "home_dir" in paths:
            home_dir = paths["home_dir"]
        if cfg_dir is None and "cfg_dir" in paths:
            cfg_dir = paths["cfg_dir"]
        if tmp_dir is None and "tmp_dir" in paths:
            tmp_dir = paths["tmp_dir"]

    base_parent = resolved_workdir.parent
    resolved_home = Path(home_dir) if home_dir is not None else (base_parent / "home")
    resolved_cfg = Path(cfg_dir) if cfg_dir is not None else (base_parent / "cfg")
    resolved_tmp = Path(tmp_dir) if tmp_dir is not None else (base_parent / "tmp")

    if isolation not in ("local", "docker"):
        raise ValueError(f"Unsupported isolation mode: {isolation}")

    source_env: dict[str, str] = dict(os.environ)
    if env_overrides:
        source_env.update(env_overrides)

    local_engine_vars: dict[str, str] = {}
    if engine.is_local:
        local_engine_vars = _resolve_local_engine_env(engine, source_env)

    def _build_claude_tail(executable: str) -> list[str]:
        """Build the claude CLI argument list for either host or container execution."""
        if engine.cmd is not None:
            return format_command(
                engine.cmd,
                prompt=prompt,
                prompt_file=prompt_file,
                role_model=engine.model,
            )
        tail: list[str] = [
            executable,
            "-p",
            *build_permission_args(allowed_tools, disallowed_tools),
            "--model",
            engine.model,
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if not skills_enabled:
            tail.append("--disable-slash-commands")
        if append_system_prompt:
            tail.extend(["--append-system-prompt", append_system_prompt])
        if output_format == "stream-json":
            tail.extend(["--output-format", "stream-json", "--verbose"])
        else:
            tail.extend(["--output-format", output_format])
        tail.append(prompt)
        return tail

    if isolation == "local":
        argv = _build_claude_tail(agent_bin)
        env: dict[str, str] = {
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "HOME": str(resolved_home),
            "CLAUDE_CONFIG_DIR": str(resolved_cfg),
            "TMPDIR": str(resolved_tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        if engine.is_local:
            env.update(local_engine_vars)
            env.pop("ANTHROPIC_API_KEY", None)
        return argv, env

    # Docker isolation mode
    docker_env_flags: list[str] = [
        "-e",
        "HOME=/workspace/home",
        "-e",
        "CLAUDE_CONFIG_DIR=/workspace/cfg",
    ]
    if engine.is_local:
        for var_name in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
        ):
            docker_env_flags.extend(["-e", var_name])

    argv = [
        docker_bin,
        "run",
        "--rm",
        "--network",
        "default",
        "--user",
        "1000:1000",
        "--workdir",
        DOCKER_TASK_DIR,
        *docker_env_flags,
        "-v",
        f"{resolved_workdir}:{DOCKER_TASK_DIR}:rw",
        "-v",
        f"{resolved_cfg}:/workspace/cfg:ro",
        *(["-v", f"{resolved_workdir / '.git'}:{DOCKER_TASK_DIR}/.git:ro"]
          if isinstance(request_or_engine, AgentRequest) and request_or_engine.role == "broker-worker" else []),
        image_tag,
        *_build_claude_tail(agent_bin_in_container),
    ]
    env = {
        "PATH": source_env.get("PATH", "/usr/bin:/bin"),
        "HOME": str(resolved_home),
        "TMPDIR": str(resolved_tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if engine.is_local:
        env.update(local_engine_vars)
        env.pop("ANTHROPIC_API_KEY", None)
    return argv, env


def build_local_agent_invocation(
    agent_bin: str,
    model: str,
    prompt_text: str,
    paths: Mapping[str, Path],
    *,
    skills_enabled: bool = False,
    engine: EngineConfig | None = None,
    output_format: str = "stream-json",
) -> tuple[list[str], dict[str, str], Path]:
    """Construct ``(cmd, env, cwd)`` for local isolation mode."""
    eng = engine or EngineConfig(name=model, model=model, engine_class="cloud")
    cmd, env = build_invocation(
        eng,
        prompt=prompt_text,
        paths=paths,
        isolation="local",
        skills_enabled=skills_enabled,
        agent_bin=agent_bin,
        output_format=output_format,
    )
    return cmd, env, paths["work_dir"]


def build_docker_agent_invocation(
    docker_bin: str,
    image_tag: str,
    model: str,
    prompt_text: str,
    paths: Mapping[str, Path],
    agent_bin_in_container: str = "claude",
    *,
    skills_enabled: bool = False,
    engine: EngineConfig | None = None,
    output_format: str = "stream-json",
) -> tuple[list[str], dict[str, str], Path]:
    """Construct ``(cmd, env, cwd)`` for docker isolation mode."""
    eng = engine or EngineConfig(name=model, model=model, engine_class="cloud")
    cmd, env = build_invocation(
        eng,
        prompt=prompt_text,
        paths=paths,
        isolation="docker",
        skills_enabled=skills_enabled,
        docker_bin=docker_bin,
        image_tag=image_tag,
        agent_bin_in_container=agent_bin_in_container,
        output_format=output_format,
    )
    return cmd, env, paths["work_dir"]


def json_string_leaves(value: Any, min_len: int = 8) -> list[str]:
    """Return every string value (length >= ``min_len``) found inside a JSON structure."""
    if isinstance(value, str):
        return [value] if len(value) >= min_len else []
    if isinstance(value, list):
        return [leaf for item in value for leaf in json_string_leaves(item, min_len)]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in json_string_leaves(item, min_len)]
    return []


def _collect_env_auth_secrets() -> list[str]:
    """Collect non-empty secret values from auth/token/key environment variables."""
    secrets: list[str] = []
    for key, val in os.environ.items():
        upper = key.upper()
        if upper in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "DEVPACK_LOCAL_AUTH_TOKEN") or any(
            upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES
        ):
            cleaned = val.strip()
            if len(cleaned) >= 4:
                secrets.append(cleaned)
    return secrets


def redact_sensitive_text(
    text: str,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
) -> str:
    """Redact credentials, auth env values, secret/PII patterns, and host home paths from ``text``."""
    if not text:
        return text
    result = text

    all_secrets: list[str] = list(extra_secrets) + _collect_env_auth_secrets()
    for secret in all_secrets:
        cleaned = secret.strip()
        if not cleaned:
            continue
        result = result.replace(cleaned, "<REDACTED_CREDENTIALS>")
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                for leaf in json_string_leaves(json.loads(cleaned)):
                    if leaf:
                        result = result.replace(leaf, "<REDACTED_CREDENTIALS>")
            except json.JSONDecodeError:
                pass

    for rule_id, rx in _get_secret_pii_rules():
        result = rx.sub(f"<REDACTED:{rule_id}>", result)

    for raw_path, placeholder in path_replacements:
        if raw_path:
            result = result.replace(raw_path, placeholder)

    env_home = os.environ.get("HOME", "").strip()
    if env_home and env_home != "/":
        result = result.replace(env_home, home_placeholder)

    try:
        resolved_home = str(Path.home())
        if resolved_home and resolved_home != "/":
            result = result.replace(resolved_home, home_placeholder)
    except RuntimeError:
        pass

    result = _HOST_HOME_RE.sub(home_placeholder, result)
    return result


def redact_structure(
    value: Any,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
) -> Any:
    """Recursively redact sensitive strings in a JSON-serializable structure."""
    if isinstance(value, str):
        return redact_sensitive_text(value, extra_secrets, path_replacements, home_placeholder)
    if isinstance(value, list):
        return [
            redact_structure(item, extra_secrets, path_replacements, home_placeholder)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            redact_sensitive_text(str(k), extra_secrets, path_replacements, home_placeholder): redact_structure(
                v, extra_secrets, path_replacements, home_placeholder
            )
            for k, v in value.items()
        }
    return value


def cleanup_cfg_dir(cfg_dir: Path) -> None:
    """Delete the credentials copy and every other entry inside ``cfg_dir``."""
    try:
        (cfg_dir / ".credentials.json").unlink(missing_ok=True)
    except OSError:
        pass
    if not cfg_dir.is_dir():
        return
    for entry in cfg_dir.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


def _looks_like_json_tool_call(text: str) -> bool:
    """Return True when assistant plain text contains a JSON-formatted tool call payload."""
    if not text or "{" not in text:
        return False

    candidates: list[str] = [m.group(1) for m in _FENCED_JSON_RE.finditer(text)]
    if _INLINE_JSON_TOOL_RE.search(text):
        return True

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    tool_name_keys = {"name", "tool", "tool_name", "function"}
    tool_arg_keys = {"arguments", "input", "parameters", "args", "params"}
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            keys = set(parsed.keys())
            if (keys & tool_name_keys) and (keys & tool_arg_keys):
                return True
            if parsed.get("type") == "tool_use" and "name" in parsed:
                return True
    return False


def parse_stream(path_or_text: Path | str) -> AgentResult:
    """Parse a Claude ``stream-json`` transcript file (or raw string) into an ``AgentResult``.

    Extracts tool call counts by tool name, detects ``tool_protocol_errors`` (assistant
    messages that print a JSON tool call as plain text without emitting any ``tool_use``
    content block), and reads final cost, token usage, turn count, duration, and text
    from the ``{"type": "result", ...}`` event.
    """
    transcript_path = Path()
    if isinstance(path_or_text, Path):
        transcript_path = path_or_text
        raw_text = path_or_text.read_text(encoding="utf-8") if path_or_text.is_file() else ""
    else:
        candidate_path = Path(path_or_text)
        if "\n" not in path_or_text and not path_or_text.lstrip().startswith("{") and candidate_path.is_file():
            transcript_path = candidate_path
            raw_text = candidate_path.read_text(encoding="utf-8")
        else:
            raw_text = str(path_or_text)

    tool_calls: dict[str, int] = {}
    seen_tool_ids: set[str] = set()
    tool_protocol_errors = 0
    assistant_messages_seen = 0
    last_assistant_text = ""
    detected_model = ""
    summed_in_tokens = 0
    summed_out_tokens = 0
    permission_denials: list[Any] = []

    result_event: dict[str, Any] | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        ev_type = event.get("type")
        if ev_type == "system" and event.get("subtype") == "init":
            if isinstance(event.get("model"), str) and event["model"]:
                detected_model = event["model"]

        elif ev_type == "assistant":
            assistant_messages_seen += 1
            msg = event.get("message")
            if not isinstance(msg, dict):
                msg = event
            if isinstance(msg.get("model"), str) and msg["model"]:
                detected_model = msg["model"]
            usage = msg.get("usage")
            if isinstance(usage, dict):
                if isinstance(usage.get("input_tokens"), int):
                    summed_in_tokens += usage["input_tokens"]
                if isinstance(usage.get("output_tokens"), int):
                    summed_out_tokens += usage["output_tokens"]

            content = msg.get("content")
            has_tool_use = False
            text_chunks: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        has_tool_use = True
                        tool_id = str(block.get("id") or "")
                        if tool_id and tool_id in seen_tool_ids:
                            continue
                        if tool_id:
                            seen_tool_ids.add(tool_id)
                        tname = str(block.get("name") or "unknown")
                        tool_calls[tname] = tool_calls.get(tname, 0) + 1
                    elif btype == "text":
                        txt = block.get("text")
                        if isinstance(txt, str) and txt:
                            text_chunks.append(txt)

            combined_text = "\n".join(text_chunks).strip()
            if combined_text:
                last_assistant_text = combined_text
                if not has_tool_use and _looks_like_json_tool_call(combined_text):
                    tool_protocol_errors += 1

        elif ev_type == "result" or (
            ev_type is None
            and any(k in event for k in ("result", "total_cost_usd", "cost_usd", "num_turns"))
        ):
            result_event = event

    if result_event is None:
        return AgentResult(
            status="harness_error",
            exit_code=1,
            final_text=last_assistant_text,
            cost_usd=0.0,
            tokens_in=summed_in_tokens,
            tokens_out=summed_out_tokens,
            duration_s=0.0,
            num_turns=0,
            tool_calls=tool_calls,
            transcript_path=transcript_path,
            tool_protocol_errors=tool_protocol_errors,
            error="no result event in stream (agent output JSON is missing or unparsable)",
            model=detected_model,
        )

    final_text = ""
    if isinstance(result_event.get("result"), str):
        final_text = result_event["result"]
    elif last_assistant_text:
        final_text = last_assistant_text

    if assistant_messages_seen == 0 and not tool_calls and _looks_like_json_tool_call(final_text):
        tool_protocol_errors += 1

    cost_usd = 0.0
    for cost_key in ("total_cost_usd", "cost_usd", "cost"):
        val = result_event.get(cost_key)
        if isinstance(val, (int, float)):
            cost_usd = float(val)
            break

    num_turns = 0
    for turns_key in ("num_turns", "turns"):
        val = result_event.get(turns_key)
        if isinstance(val, int):
            num_turns = val
            break

    duration_s = 0.0
    if isinstance(result_event.get("duration_s"), (int, float)):
        duration_s = float(result_event["duration_s"])
    elif isinstance(result_event.get("duration_ms"), (int, float)):
        duration_s = float(result_event["duration_ms"]) / 1000.0

    tokens_in = summed_in_tokens
    tokens_out = summed_out_tokens
    usage = result_event.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("input_tokens"), int):
            tokens_in = int(usage["input_tokens"])
        if isinstance(usage.get("output_tokens"), int):
            tokens_out = int(usage["output_tokens"])

    model_usage = result_event.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage and not detected_model:
        detected_model = str(next(iter(model_usage.keys())))

    denials = result_event.get("permission_denials")
    if isinstance(denials, list):
        permission_denials = denials

    is_error = bool(result_event.get("is_error", False)) or (
        result_event.get("subtype") in ("error", "error_max_turns", "error_during_execution")
    )
    status = "agent_error" if is_error else "ok"
    error_msg = final_text if is_error else ""

    return AgentResult(
        status=status,
        exit_code=1 if is_error else 0,
        final_text=final_text,
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_s=duration_s,
        num_turns=num_turns,
        tool_calls=tool_calls,
        transcript_path=transcript_path,
        tool_protocol_errors=tool_protocol_errors,
        error=error_msg,
        permission_denials=permission_denials,
        model=detected_model,
    )


def extract_agent_json_object(stdout_text: str) -> dict[str, Any] | None:
    """Return the final result JSON object printed by the agent, or None when unparsable."""
    stripped = stdout_text.strip()
    if not stripped:
        return None

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    candidates = list(reversed(lines)) if len(lines) > 1 else [stripped]

    fallback: dict[str, Any] | None = None
    for candidate in candidates:
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if obj.get("type") == "result" or any(
                k in obj for k in ("result", "total_cost_usd", "cost_usd", "num_turns")
            ):
                return obj
            if fallback is None:
                fallback = obj
    return fallback


def parse_agent_json_output(stdout_text: str) -> dict[str, Any]:
    """Parse cost, turns, and duration telemetry from agent JSON or stream-json output."""
    metrics: dict[str, Any] = {
        "cost_usd": None,
        "turns": None,
        "duration_s": None,
    }
    parsed = extract_agent_json_object(stdout_text)
    if not parsed:
        return metrics

    for cost_key in ("total_cost_usd", "cost_usd", "cost"):
        val = parsed.get(cost_key)
        if isinstance(val, (int, float)):
            metrics["cost_usd"] = float(val)
            break

    for turns_key in ("num_turns", "turns"):
        val = parsed.get(turns_key)
        if isinstance(val, int):
            metrics["turns"] = val
            break

    if isinstance(parsed.get("duration_s"), (int, float)):
        metrics["duration_s"] = float(parsed["duration_s"])
    elif isinstance(parsed.get("duration_ms"), (int, float)):
        metrics["duration_s"] = float(parsed["duration_ms"]) / 1000.0

    return metrics


def detect_harness_error(
    agent_rc: int,
    stdout_text: str,
    stderr_text: str,
    timeout_error: str = "",
) -> str:
    """Return a reason string when the run is a harness error, else an empty string.

    Note: ``[claude-code:unrecognized_model]`` on stderr is a benign warning for local
    models and is never classified as a harness error.
    """
    if timeout_error:
        return timeout_error
    combined = f"{stdout_text}\n{stderr_text}"
    lowered = combined.lower()
    if "not been trusted" in lowered:
        return "workspace not trusted: agent ignored project permissions"
    if "ignoring" in lowered and "permissions" in lowered:
        return "agent ignored permissions entries"
    for pattern in _AUTH_FAILURE_PATTERNS:
        if pattern in lowered:
            return f"authentication failure: {pattern}"
    if agent_rc != 0:
        return f"agent exited with non-zero status {agent_rc}"
    result_obj = extract_agent_json_object(stdout_text)
    if result_obj is None or (
        result_obj.get("type") != "result"
        and not any(k in result_obj for k in ("result", "total_cost_usd", "cost_usd", "num_turns"))
    ):
        return "no result event in stream: agent output JSON is missing or unparsable"
    return ""


def new_run_dir(
    runs_root: Path | str,
    kind: str = "task",
    *,
    task: str = "",
    engines: Sequence[str] | Mapping[str, Any] | None = None,
    outcome: str = "running",
    run_id: str | None = None,
) -> Path:
    """Create a new ``runs/<run-id>/`` directory with ``steps/`` and initial ``run.json``."""
    now = datetime.now(timezone.utc)
    if not run_id:
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    root_path = Path(runs_root)
    run_dir = root_path / run_id
    (run_dir / "steps").mkdir(parents=True, exist_ok=True)

    if isinstance(engines, Mapping):
        engines_val: Any = dict(engines)
    elif isinstance(engines, (list, tuple, set)):
        engines_val = list(engines)
    else:
        engines_val = []

    run_payload = {
        "run_id": run_id,
        "kind": kind,
        "started": now.isoformat(),
        "finished": None,
        "engines": engines_val,
        "task": task,
        "outcome": outcome,
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir


def update_run_json(run_dir: Path | str, **updates: Any) -> dict[str, Any]:
    """Update fields in ``<run_dir>/run.json`` (setting ``finished`` if ``outcome`` finishes)."""
    rdir = Path(run_dir)
    run_json_path = rdir / "run.json"
    if run_json_path.is_file():
        try:
            payload = json.loads(run_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {
            "run_id": rdir.name,
            "kind": "task",
            "started": datetime.now(timezone.utc).isoformat(),
            "finished": None,
            "engines": [],
            "task": "",
            "outcome": "running",
        }
    payload.update(updates)
    if "outcome" in updates and updates["outcome"] != "running" and not payload.get("finished"):
        payload["finished"] = datetime.now(timezone.utc).isoformat()
    rdir.mkdir(parents=True, exist_ok=True)
    run_json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _resolve_step_stem(request: AgentRequest) -> str:
    """Compute the ``NN-<role>-<engine>`` filename stem for a step inside ``steps/``."""
    raw = request.step_name.strip()
    for suffix in (".jsonl", ".meta.json", ".prompt.md"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    if raw and _STEP_PREFIX_RE.match(raw):
        parts = raw.split("-")
        if len(parts) >= 3:
            return raw
        return f"{raw}-{request.engine.name}"
    role = raw if raw else (request.role or "agent")
    return f"{request.step_index:02d}-{role}-{request.engine.name}"


def run_agent(request: AgentRequest) -> AgentResult:
    """Launch an agent in isolation, stream redacted JSONL to ``steps/``, and return ``AgentResult``.

    Writes ``steps/NN-<role>-<engine>.{prompt.md,jsonl,meta.json}``, always feeds
    ``/dev/null`` on stdin, and guarantees deletion of the temporary credentials copy
    in a ``finally`` block.
    """
    steps_dir = request.run_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    if not (request.run_dir / "run.json").is_file():
        update_run_json(
            request.run_dir,
            run_id=request.run_dir.name,
            kind="task",
            engines=[request.engine.name],
            outcome="running",
        )

    stem = _resolve_step_stem(request)
    jsonl_path = steps_dir / f"{stem}.jsonl"
    meta_path = steps_dir / f"{stem}.meta.json"
    prompt_path = steps_dir / f"{stem}.prompt.md"
    prompt_sha256 = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()

    # Collect all secrets to redact before writing any files
    secrets_list: list[str] = list(request.extra_secrets)
    source_env: dict[str, str] = dict(os.environ)
    if request.env_overrides:
        source_env.update(request.env_overrides)
    if request.engine.auth_token_env:
        tok_val = source_env.get(request.engine.auth_token_env, "").strip()
        if tok_val:
            secrets_list.append(tok_val)

    cred_path = request.credentials_path
    if cred_path is None and request.engine.is_cloud:
        env_cred = source_env.get("DEVPACK_CLAUDE_CREDENTIALS", "").strip()
        if env_cred:
            cred_path = Path(env_cred)

    if cred_path is not None and cred_path.is_file():
        try:
            cred_text = cred_path.read_text(encoding="utf-8").strip()
            if cred_text:
                secrets_list.extend([cred_text, str(cred_path)])
                try:
                    secrets_list.extend(json_string_leaves(json.loads(cred_text)))
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass

    def _redact(text: str) -> str:
        """Redact secrets and home paths using request settings."""
        return redact_sensitive_text(
            text,
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
        )

    prompt_path.write_text(_redact(request.prompt), encoding="utf-8")

    def _write_meta_and_return(res: AgentResult) -> AgentResult:
        """Populate metadata fields, write .meta.json, and return the result."""
        res.transcript_path = jsonl_path
        res.model = res.model or request.engine.model
        res.engine = request.engine.name
        res.role = request.role
        res.prompt_sha256 = prompt_sha256
        res.prompt_file = prompt_path.name
        res.final_text = _redact(res.final_text)
        res.error = _redact(res.error)
        res.stderr = _redact(res.stderr)
        res.permission_denials = redact_structure(
            res.permission_denials,
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
        )
        meta_payload = redact_structure(
            res.to_dict(),
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
        )
        meta_path.write_text(json.dumps(meta_payload, indent=2) + "\n", encoding="utf-8")
        return res

    iso_allowed, iso_msg = check_local_isolation_allowed(
        request.isolation,
        allow_unsandboxed_local=request.allow_unsandboxed_local,
        workdir=request.workdir,
    )
    if not iso_allowed:
        sys.stderr.write(iso_msg + "\n")
        jsonl_path.write_text("", encoding="utf-8")
        update_run_json(request.run_dir, isolation=request.isolation, outcome="refused")
        return _write_meta_and_return(
            AgentResult(
                status="harness_error",
                exit_code=126,
                error=iso_msg,
            )
        )
    if request.isolation == "local" and iso_msg:
        sys.stderr.write(iso_msg + "\n")
        update_run_json(request.run_dir, isolation="local", sandbox_warning=iso_msg)

    # Set up temporary isolation directories if not already provided by caller
    created_temp_iso: Path | None = None
    if request.cfg_dir is not None and request.home_dir is not None and request.tmp_dir is not None:
        home_dir = request.home_dir
        cfg_dir = request.cfg_dir
        tmp_dir = request.tmp_dir
    else:
        created_temp_iso = Path(tempfile.mkdtemp(prefix="devpack-agent-iso-"))
        home_dir = request.home_dir or (created_temp_iso / "home")
        cfg_dir = request.cfg_dir or (created_temp_iso / "cfg")
        tmp_dir = request.tmp_dir or (created_temp_iso / "tmp")

    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    request.workdir.mkdir(parents=True, exist_ok=True)

    try:
        # Handle credentials for cloud engines vs local engines
        if request.engine.is_cloud:
            if cred_path is not None:
                if not cred_path.is_file():
                    jsonl_path.write_text("", encoding="utf-8")
                    return _write_meta_and_return(
                        AgentResult(
                            status="harness_error",
                            exit_code=126,
                            error=f"Credentials file not found: {cred_path}",
                        )
                    )
                cred_dest = cfg_dir / ".credentials.json"
                if cred_path.resolve() != cred_dest.resolve():
                    shutil.copyfile(cred_path, cred_dest)
                os.chmod(cred_dest, stat.S_IRUSR | stat.S_IWUSR)
            elif (cfg_dir / ".credentials.json").is_file():
                try:
                    existing_cred = (cfg_dir / ".credentials.json").read_text(encoding="utf-8").strip()
                    if existing_cred:
                        secrets_list.append(existing_cred)
                        secrets_list.extend(json_string_leaves(json.loads(existing_cred)))
                except (OSError, json.JSONDecodeError):
                    pass
        else:
            # Local engines never copy credentials
            (cfg_dir / ".credentials.json").unlink(missing_ok=True)

        agent_cwd = (
            DOCKER_TASK_DIR
            if request.isolation == "docker"
            else str(request.workdir.resolve())
        )
        state_path = cfg_dir / ".claude.json"
        state_path.write_text(
            json.dumps(build_claude_state(agent_cwd, include_onboarding=True), indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            argv, env = build_invocation(
                request,
                home_dir=home_dir,
                cfg_dir=cfg_dir,
                tmp_dir=tmp_dir,
                prompt_file=prompt_path,
            )
        except (HarnessError, ValueError) as exc:
            jsonl_path.write_text("", encoding="utf-8")
            return _write_meta_and_return(
                AgentResult(
                    status="harness_error",
                    exit_code=126,
                    error=str(exc),
                )
            )

        timeout_s = request.timeout
        start_ts = time.monotonic()
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []
        timed_out = False
        os_error_msg = ""
        exit_code = -1

        try:
            with jsonl_path.open("w", encoding="utf-8") as jsonl_fh:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(request.workdir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                def _read_stdout() -> None:
                    """Read subprocess stdout line-by-line, redact, and flush to jsonl_fh."""
                    assert proc.stdout is not None
                    for raw_line in proc.stdout:
                        redacted_line = _redact(raw_line)
                        stdout_lines.append(redacted_line)
                        jsonl_fh.write(redacted_line)
                        if not redacted_line.endswith("\n"):
                            jsonl_fh.write("\n")
                        jsonl_fh.flush()

                def _read_stderr() -> None:
                    """Read subprocess stderr into memory."""
                    assert proc.stderr is not None
                    for raw_line in proc.stderr:
                        stderr_chunks.append(_redact(raw_line))

                t_out = threading.Thread(target=_read_stdout, daemon=True)
                t_err = threading.Thread(target=_read_stderr, daemon=True)
                t_out.start()
                t_err.start()

                try:
                    exit_code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = 124
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

                t_out.join(timeout=5)
                t_err.join(timeout=5)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
        except OSError as exc:
            exit_code = 127
            os_error_msg = f"Failed to execute agent CLI '{ argv[0] if argv else request.agent_bin }': {exc}"
            if not jsonl_path.is_file():
                jsonl_path.write_text("", encoding="utf-8")

        elapsed_s = round(time.monotonic() - start_ts, 4)
        stdout_full = "".join(stdout_lines)
        stderr_full = "".join(stderr_chunks)

        if os_error_msg:
            return _write_meta_and_return(
                AgentResult(
                    status="harness_error",
                    exit_code=exit_code,
                    duration_s=elapsed_s,
                    error=os_error_msg,
                    stderr=stderr_full,
                )
            )

        parsed = parse_stream(jsonl_path)
        parsed.exit_code = exit_code
        parsed.stderr = stderr_full
        if parsed.duration_s <= 0.0:
            parsed.duration_s = elapsed_s

        if timed_out:
            parsed.status = "timeout"
            parsed.exit_code = 124
            parsed.error = f"Agent timed out after {timeout_s}s"
            return _write_meta_and_return(parsed)

        # Support legacy v0.1 custom commands that print plain text rather than stream-json
        is_legacy_plain_cmd = (
            request.engine.cmd is not None
            and "stream-json" not in request.engine.cmd
            and "json" not in request.engine.cmd
        )
        if is_legacy_plain_cmd and parsed.status == "harness_error" and "no result event" in parsed.error:
            if exit_code == 0:
                parsed.status = "ok"
                parsed.final_text = stdout_full.strip()
                parsed.error = ""
            else:
                parsed.status = "agent_error"
                parsed.final_text = stdout_full.strip()
                parsed.error = stderr_full.strip() or f"Command exited with {exit_code}"
            return _write_meta_and_return(parsed)

        # Check for trust or auth errors in stdout/stderr
        combined_lower = f"{stdout_full}\n{stderr_full}".lower()
        if "not been trusted" in combined_lower:
            parsed.status = "harness_error"
            parsed.error = "workspace not trusted: agent ignored project permissions"
            return _write_meta_and_return(parsed)
        if "ignoring" in combined_lower and "permissions" in combined_lower:
            parsed.status = "harness_error"
            parsed.error = "agent ignored permissions entries"
            return _write_meta_and_return(parsed)
        for pattern in _AUTH_FAILURE_PATTERNS:
            if pattern in combined_lower:
                parsed.status = "harness_error"
                parsed.error = f"authentication failure: {pattern}"
                return _write_meta_and_return(parsed)

        if parsed.status == "harness_error":
            if exit_code != 0:
                parsed.error = (
                    stderr_full.strip()
                    or f"agent exited with non-zero status {exit_code} (no result event)"
                )
                if "non-zero" not in parsed.error:
                    parsed.error = (
                        f"agent exited with non-zero status {exit_code}: {parsed.error}"
                    )
            return _write_meta_and_return(parsed)

        if exit_code != 0 and parsed.status == "ok":
            parsed.status = "agent_error"
            parsed.error = stderr_full.strip() or f"agent exited with non-zero status {exit_code}"

        return _write_meta_and_return(parsed)
    finally:
        cleanup_cfg_dir(cfg_dir)
        if created_temp_iso is not None:
            shutil.rmtree(created_temp_iso, ignore_errors=True)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/agent_exec.py``."""
    parser = argparse.ArgumentParser(
        prog="agent_exec.py",
        description=(
            "Single isolated agent executor, v2/v0.1 engine configuration loader, "
            "and stream-json transcript parser."
        ),
    )
    parser.add_argument(
        "--check-config",
        default="",
        metavar="PATH",
        help="Validate and print a summary of an engines.toml file",
    )
    parser.add_argument(
        "--parse-stream",
        default="",
        metavar="PATH",
        help="Parse a stream-json .jsonl transcript and print the resulting AgentResult JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/agent_exec.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        try:
            cfg = load_engines(Path(args.check_config))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "version": cfg.version,
                    "engines": {k: v.to_dict() for k, v in cfg.engines.items()},
                    "roles": cfg.roles,
                    "limits": cfg.limits,
                },
                indent=2,
            )
        )
        return 0

    if args.parse_stream:
        stream_path = Path(args.parse_stream)
        if not stream_path.is_file():
            print(f"error: stream file not found: {stream_path}", file=sys.stderr)
            return 2
        res = parse_stream(stream_path)
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.status == "ok" else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
