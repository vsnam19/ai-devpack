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
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

try:
    import grp  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - non-POSIX
    grp = None  # type: ignore[assignment]

try:
    import pwd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None  # type: ignore[assignment]

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

try:
    from runner import memory as devpack_memory  # type: ignore[import-not-found]
    from runner.redaction import (  # type: ignore[import-not-found]
        NO_OS_IDENTITIES as _NO_OS_IDENTITIES,
        collect_env_auth_secrets as _collect_env_auth_secrets,
        collect_os_identities,
        json_string_leaves,
        redact_os_identities,
        redact_sensitive_text,
        redact_stream_line,
        redact_structure,
        redact_value,
    )
    from runner.safe_git import GitSecurityError, run_git  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    import memory as devpack_memory  # type: ignore[no-redef]
    from redaction import (  # type: ignore[no-redef]
        NO_OS_IDENTITIES as _NO_OS_IDENTITIES,
        collect_env_auth_secrets as _collect_env_auth_secrets,
        collect_os_identities,
        json_string_leaves,
        redact_os_identities,
        redact_sensitive_text,
        redact_stream_line,
        redact_structure,
        redact_value,
    )
    from safe_git import GitSecurityError, run_git  # type: ignore[no-redef]

DEFAULT_CLAUDE_VERSION = "2.1.330"
DOCKER_IMAGE_TAG = f"devpack-eval:{DEFAULT_CLAUDE_VERSION}"
DOCKER_TASK_DIR = "/workspace/task"
_GIT_COMMON_DIR_TIMEOUT_S = 30
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
ORCHESTRATOR_TOOLS: tuple[str, ...] = (
    "Read", "Glob", "Grep", "Write(briefs/**)", "Edit(briefs/**)",
    "Bash(python3 .devpack/pack/runner/devpack_dispatch.py --submit:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
)
ROLE_TOOLS = {"reviewer": ("Read", "Glob", "Grep"), "orchestrator": ORCHESTRATOR_TOOLS}
DISALLOWED_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
DEFAULT_GATE_CMD: tuple[str, ...] = (
    "python3",
    str(Path(__file__).resolve().parents[1] / "enforcement/gate/gate.py"),
    "--config",
    ".devpack/project.toml:gate_config",
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
WORKDIR_SANDBOX_OPT_OUT_WARNING = (
    "WARNING: ignoring allow_unsandboxed_local in workdir .devpack/project.toml "
    "(repo-controlled sandbox opt-out is disabled; use --allow-unsandboxed-local "
    "or DEVPACK_OPERATOR_CONFIG)."
)
OPERATOR_CONFIG_ENV = "DEVPACK_OPERATOR_CONFIG"

_STEP_PREFIX_RE = re.compile(r"^\d{2,}-.+")
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_INLINE_JSON_TOOL_RE = re.compile(
    r"\{\s*\"(?:name|tool|tool_name|function)\"\s*:\s*\"[^\"]+\"\s*,"
    r"\s*\"(?:arguments|input|parameters|args|params)\"\s*:\s*\{.*?\}\s*\}",
    re.DOTALL,
)
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
    role_permissions: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

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
        self.role_permissions = {}

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
class MemoryConfig:
    """Configuration for bounded project memory retrieval and injection."""

    enabled: bool = True
    top_k: int = 5
    budget_tokens: int = 800
    reviewer_sees_handoffs: bool = False
    role_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
    memory: MemoryConfig = field(default_factory=MemoryConfig)

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
        if key in ("version", "engines", "roles", "limits", "max_fix_rounds", "gate_cmd", "gate_timeout_s", "memory"):
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
            or key in ("version", "engines", "roles", "limits", "max_fix_rounds", "gate_cmd", "gate_timeout_s", "memory")
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
        if raw.startswith("[[") and raw.endswith("]]"):
            section_path = [p.strip() for p in raw[2:-2].strip().split(".") if p.strip()]
            target = root
            for part in section_path[:-1]:
                target = target.setdefault(part, {})
            arr = target.setdefault(section_path[-1], [])
            entry: dict[str, Any] = {}
            arr.append(entry)
            current = entry
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


def _parse_memory_config(data: Mapping[str, Any]) -> MemoryConfig:
    """Parse optional ``[memory]`` configuration table from engines.toml."""
    raw_mem = data.get("memory", {})
    if raw_mem is None:
        return MemoryConfig()
    if not isinstance(raw_mem, Mapping):
        raise ValueError("[memory] must be a TOML table")
    enabled = bool(raw_mem.get("enabled", True))
    top_k = int(raw_mem.get("top_k", 5))
    budget_tokens = int(raw_mem.get("budget_tokens", 800))
    if top_k < 0 or budget_tokens < 0:
        raise ValueError("[memory] top_k and budget_tokens must be >= 0")
    reviewer_sees_handoffs = bool(raw_mem.get("reviewer_sees_handoffs", False))
    role_scopes: dict[str, tuple[str, ...]] = {}
    raw_role_scopes = raw_mem.get("role_scopes", {})
    if isinstance(raw_role_scopes, Mapping):
        for role_k, scopes_v in raw_role_scopes.items():
            if isinstance(scopes_v, list):
                role_scopes[str(role_k)] = tuple(str(x).strip() for x in scopes_v if str(x).strip())
    return MemoryConfig(
        enabled=enabled,
        top_k=top_k,
        budget_tokens=budget_tokens,
        reviewer_sees_handoffs=reviewer_sees_handoffs,
        role_scopes=role_scopes,
    )


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

    role_permissions = {}
    raw_policy_roles = data.get("roles", {})
    if not isinstance(raw_policy_roles, dict):
        raise ValueError("[roles] must be a table")
    for role, spec in raw_policy_roles.items():
        if isinstance(spec, dict):
            policy = {}
            for key in ("allowed_tools", "disallowed_tools"):
                if key in spec:
                    value = spec[key]
                    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                        raise ValueError(f"Role '{role}' {key} must be a list of nonempty strings")
                    policy[key] = tuple(value)
            role_permissions[role] = policy

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
            elif isinstance(target, dict) and "engine" in target:
                selected = target["engine"]
                if not isinstance(selected, str) or selected not in engines:
                    raise ValueError(f"Role '{role_name}' references undefined engine '{selected}'")
                roles[role_name] = selected
                role_engines[role_name] = engines[selected]
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

        for engine in [*engines.values(), *role_engines.values()]:
            engine.role_permissions = role_permissions

        memory_cfg = _parse_memory_config(data)
        return EnginesConfig(
            version=int(data.get("version", 2)),
            engines=engines,
            roles=roles,
            limits=limits,
            max_fix_rounds=max_fix_rounds,
            gate_cmd=gate_cmd,
            gate_timeout_s=gate_timeout_s,
            role_engines=role_engines,
            memory=memory_cfg,
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

        for engine in [*engines.values(), *role_engines.values()]:
            engine.role_permissions = role_permissions

        memory_cfg = _parse_memory_config(data)
        return EnginesConfig(
            version=1,
            engines=engines,
            roles=roles,
            limits=limits,
            max_fix_rounds=max_fix_rounds,
            gate_cmd=gate_cmd,
            gate_timeout_s=gate_timeout_s,
            role_engines=role_engines,
            memory=memory_cfg,
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
        "engines_config": str(Path(__file__).resolve().parent / "engines.example.toml"),
        "gate_config": "",
        "runs_root": "runs",
        "default_engines": {},
        "allow_unsandboxed_local": False,
        "ignored_allow_unsandboxed_local": False,
        "require_qualification": False,
        "workspace": {},
        "components": [],
        "scan": {},
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
    for key in ("engines_config", "gate_config"):
        value = str(raw.get(key, defaults[key])).strip()
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                project_candidate = cfg_path.resolve().parent / candidate
                pack_candidate = Path(__file__).resolve().parents[1] / candidate
                candidate = project_candidate if project_candidate.exists() or not pack_candidate.exists() else pack_candidate
            defaults[key] = str(candidate.resolve())
    defaults["runs_root"] = str(raw.get("runs_root", defaults["runs_root"])).strip()
    raw_defaults = raw.get("default_engines", {})
    if isinstance(raw_defaults, dict):
        defaults["default_engines"] = {str(k): str(v) for k, v in raw_defaults.items()}
    if bool(raw.get("allow_unsandboxed_local", False)):
        defaults["ignored_allow_unsandboxed_local"] = True
    defaults["allow_unsandboxed_local"] = False
    defaults["require_qualification"] = bool(raw.get("require_qualification", False))
    raw_ws = raw.get("workspace", {})
    if isinstance(raw_ws, dict):
        defaults["workspace"] = dict(raw_ws)
    elif "workspace" in raw:
        raise ValueError(f"[workspace] must be a table in {cfg_path}")
    raw_comp = raw.get("component", [])
    if isinstance(raw_comp, list):
        defaults["components"] = [dict(c) for c in raw_comp if isinstance(c, dict)]
    elif "component" in raw:
        raise ValueError(f"[[component]] must be an array of tables in {cfg_path}")
    raw_scan = raw.get("scan", {})
    if isinstance(raw_scan, dict):
        defaults["scan"] = dict(raw_scan)
    elif "scan" in raw:
        raise ValueError(f"[scan] must be a table in {cfg_path}")
    defaults["config_found"] = True
    return defaults


def build_gate_argv(
    gate_config: Path | str,
    repo: Path | str,
    *,
    receipt: Path | str | None = None,
    all_components: bool = True,
    components: Sequence[str] | None = None,
    project_config_path: Path | str | None = None,
    pack_root: Path | str | None = None,
) -> list[str]:
    """Build the ``enforcement/gate/gate.py`` argv shared by dispatch and the project gate.

    Scope precedence matches the lint and cppcheck invocations: ``all_components`` wins, otherwise
    a non-empty ``components`` list is forwarded as one ``--components`` value. ``project_config_path``
    is forwarded so gate.py resolves the same project settings as the caller.
    """
    root = Path(pack_root) if pack_root is not None else Path(__file__).resolve().parents[1]
    argv = [
        "python3", str(root / "enforcement" / "gate" / "gate.py"),
        "--config", str(gate_config), "--repo", str(repo),
    ]
    if receipt is not None:
        argv.extend(["--receipt", str(receipt)])
    if all_components:
        argv.append("--all-components")
    elif components:
        argv.extend(["--components", ",".join(components)])
    if project_config_path is not None and str(project_config_path):
        argv.extend(["--project-config", str(project_config_path)])
    return argv


def resolve_project_gate_command(
    workdir: Path,
    gate_config: Path | str | None = None,
    project_config_path: Path | str | None = None,
    *,
    receipt: Path | str | None = None,
    all_components: bool = True,
    components: Sequence[str] | None = None,
) -> list[str]:
    """Build a pack-absolute gate command from the project's explicit gate configuration."""
    project = load_project_config(workdir, project_config_path)
    config = Path(gate_config).resolve() if gate_config else None
    if config is None:
        raw = str(project.get("gate_config", "")).strip()
        if not raw:
            raise ValueError("Project gate_config is unset; set gate_config in .devpack/project.toml or pass --gate-config")
        config = Path(raw)
    if not config.is_file():
        raise ValueError(f"Project gate_config not found: {config}")
    return build_gate_argv(
        config,
        workdir.resolve(),
        receipt=receipt,
        all_components=all_components,
        components=components,
        project_config_path=project_config_path,
    )


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


def load_operator_config(
    operator_config_path: Path | str | None = None,
    *,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    """Load operator-owned sandbox configuration from outside ``workdir``.

    Reads ``operator_config_path`` or ``os.environ['DEVPACK_OPERATOR_CONFIG']``.
    Refuses paths inside ``workdir`` (or symlinks resolving into ``workdir``).
    """
    raw_path = (
        str(operator_config_path).strip()
        if operator_config_path is not None and str(operator_config_path).strip()
        else os.environ.get(OPERATOR_CONFIG_ENV, "").strip()
    )
    if not raw_path:
        return {"allow_unsandboxed_local": False, "config_found": False, "path": ""}

    candidate = Path(raw_path)
    resolved = candidate.resolve()
    if workdir is not None:
        wd_res = Path(workdir).resolve()
        if resolved == wd_res or resolved.is_relative_to(wd_res):
            raise ValueError(
                f"{OPERATOR_CONFIG_ENV} ({resolved}) must be outside workdir ({wd_res})"
            )
    if not resolved.is_file():
        raise ValueError(f"{OPERATOR_CONFIG_ENV} file not found: {resolved}")

    text = resolved.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            raw = tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"Failed to parse TOML in {resolved}: {exc}") from exc
    else:  # pragma: no cover
        raw = _minimal_toml_loads(text)

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid TOML operator configuration in {resolved}")

    sandbox_tbl = raw.get("sandbox")
    broker_tbl = raw.get("broker")
    allow_local = bool(raw.get("allow_unsandboxed_local", False))
    if isinstance(sandbox_tbl, dict) and bool(sandbox_tbl.get("allow_unsandboxed_local", False)):
        allow_local = True
    if isinstance(broker_tbl, dict) and bool(broker_tbl.get("allow_unsandboxed_local", False)):
        allow_local = True
    return {
        "allow_unsandboxed_local": allow_local,
        "config_found": True,
        "path": str(resolved),
    }


def detect_ignored_workdir_sandbox_opt_out(
    *,
    workdir: Path | str | None = None,
    project_config: Mapping[str, Any] | None = None,
) -> str:
    """Return warning text when a workdir ``project.toml`` attempts ``allow_unsandboxed_local``."""
    cfg = dict(project_config) if project_config is not None else {}
    if not cfg and workdir is not None:
        try:
            cfg = load_project_config(workdir)
        except ValueError:
            cfg = {}
    if bool(cfg.get("ignored_allow_unsandboxed_local", False)):
        return WORKDIR_SANDBOX_OPT_OUT_WARNING
    return ""


def check_local_isolation_allowed(
    isolation: str,
    *,
    allow_unsandboxed_local: bool = False,
    workdir: Path | str | None = None,
    project_config: Mapping[str, Any] | None = None,
    operator_config_path: Path | str | None = None,
) -> tuple[bool, str]:
    """Enforce fail-safe sandbox policy for ``isolation='local'`` (refuse unless explicitly allowed).

    Workdir ``.devpack/project.toml`` ``allow_unsandboxed_local`` is never honoured; when
    present, a warning is returned in ``message`` for recording in ``run.json``.
    """
    ignored_warn = detect_ignored_workdir_sandbox_opt_out(
        workdir=workdir,
        project_config=project_config,
    )
    if isolation == "docker":
        return True, ignored_warn
    if isolation != "local":
        raise ValueError(f"Unsupported isolation mode: {isolation}")

    try:
        op_cfg = load_operator_config(operator_config_path, workdir=workdir)
    except ValueError as exc:
        err = f"Refused: invalid operator config: {exc}"
        if ignored_warn:
            err = f"{ignored_warn}\n{err}"
        return False, err

    if allow_unsandboxed_local or bool(op_cfg.get("allow_unsandboxed_local", False)):
        msg = f"{ignored_warn}\n{LOCAL_ISOLATION_WARNING}" if ignored_warn else LOCAL_ISOLATION_WARNING
        return True, msg

    refusal = (
        "Refused: isolation='local' runs unsandboxed on the host filesystem and network "
        "(policy/sandbox.md). Use --isolation docker (default), pass "
        "--allow-unsandboxed-local, or set allow_unsandboxed_local = true in DEVPACK_OPERATOR_CONFIG."
    )
    if ignored_warn:
        refusal = f"{ignored_warn}\n{refusal}"
    return False, refusal


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


_PROFILE_VERSION_RE = re.compile(r"^\s*Version\s*:\s*(\d+)\s*$", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def parse_profile_version(text: str) -> int | None:
    """Extract the integer ``Version: N`` header from an engine profile, or None."""
    for line in text.splitlines():
        match = _PROFILE_VERSION_RE.match(line)
        if match:
            return int(match.group(1))
    return None


def extract_profile_body(text: str) -> str:
    """Return substantive guidance lines from an engine profile, stripping headers and comments."""
    cleaned = _HTML_COMMENT_RE.sub("", text)
    body_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            continue
        if stripped.startswith("# Engine Profile:"):
            continue
        if _PROFILE_VERSION_RE.match(stripped):
            continue
        body_lines.append(raw_line.rstrip())
    while body_lines and body_lines[-1] == "":
        body_lines.pop()
    return "\n".join(body_lines).strip()


def load_engine_profile(
    profile_path: Path | str | None,
) -> tuple[str, str, int | None, str]:
    """Read an engine profile from disk on demand.

    Returns ``(body_text, sha256_hex, version_or_none, file_name)``. When the file
    is absent or ``profile_path`` is None, returns ``("", "", None, "")``.
    """
    if profile_path is None or not str(profile_path):
        return "", "", None, ""
    path = Path(profile_path)
    if not path.is_file():
        return "", "", None, ""
    raw_bytes = path.read_bytes()
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8", errors="replace")
    version = parse_profile_version(text)
    body = extract_profile_body(text)
    return body, sha256_hex, version, path.name


def resolve_engine_profile_path(
    engine_name: str,
    *roots: Path | str | None,
) -> Path | None:
    """Locate ``knowledge/engine_profiles/<engine_name>.md`` across candidate roots."""
    if not engine_name:
        return None
    for root in roots:
        if root is None or not str(root):
            continue
        candidate = Path(root) / "knowledge" / "engine_profiles" / f"{engine_name}.md"
        if candidate.is_file():
            return candidate
    pack_default = Path(__file__).resolve().parent.parent / "knowledge" / "engine_profiles" / f"{engine_name}.md"
    return pack_default if pack_default.is_file() else None


def compute_engine_config_hash(engine: EngineConfig) -> str:
    """Return a deterministic SHA-256 hex digest of an ``EngineConfig`` specification."""
    payload = {
        "name": engine.name,
        "model": engine.model,
        "engine_class": engine.engine_class,
        "max_data_class": engine.max_data_class,
        "base_url_env": engine.base_url_env,
        "auth_token_env": engine.auth_token_env,
        "min_context_tokens": int(engine.min_context_tokens),
        "timeout_s": int(engine.timeout_s),
        "cmd": list(engine.cmd) if engine.cmd is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_qualification_data(qual_path: Path) -> dict[str, Any]:
    """Load ``.devpack/qualification.json`` if present, else return an empty record."""
    if not qual_path.is_file():
        return {"version": 1, "engines": {}}
    try:
        loaded = json.loads(qual_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to parse qualification file {qual_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid qualification JSON in {qual_path}: expected object")
    engines = loaded.get("engines")
    if not isinstance(engines, dict):
        engines = {}
    return {"version": int(loaded.get("version", 1)), "engines": dict(engines)}


def save_qualification_data(qual_path: Path, data: Mapping[str, Any]) -> None:
    """Persist ``qualification.json`` atomically with deterministic formatting."""
    qual_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = qual_path.with_name(f"{qual_path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(dict(data), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, qual_path)


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
    allowed_tools: Sequence[str] | None = None
    disallowed_tools: Sequence[str] | None = None
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
    profile_path: Path | None = None
    memory_enabled: bool | None = None
    memory_root: Path | None = None
    memory_top_k: int | None = None
    memory_budget_tokens: int | None = None
    memory_config: MemoryConfig | None = None
    memory_task_text: str | None = None
    memory_task_id: str = ""
    code_map: str = ""
    operator_config_path: Path | None = None

    def __init__(
        self,
        engine: EngineConfig | None = None,
        prompt: str = "",
        workdir: Path | str = ".",
        run_dir: Path | str | None = None,
        step_name: str = "",
        isolation: str = "docker",
        allow_unsandboxed_local: bool = False,
        allowed_tools: Sequence[str] | None = None,
        disallowed_tools: Sequence[str] | None = None,
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
        profile_path: Path | str | None = None,
        memory_enabled: bool | None = None,
        memory_root: Path | str | None = None,
        memory_top_k: int | None = None,
        memory_budget_tokens: int | None = None,
        memory_config: MemoryConfig | None = None,
        memory_task_text: str | None = None,
        memory_task_id: str = "",
        code_map: str = "",
        operator_config_path: Path | str | None = None,
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
        policy = self.engine.role_permissions.get(role, {})
        self.allowed_tools = tuple(allowed_tools) if allowed_tools is not None else policy.get(
            "allowed_tools", ROLE_TOOLS.get(role, ALLOWED_TOOLS))
        self.disallowed_tools = tuple(disallowed_tools) if disallowed_tools is not None else policy.get(
            "disallowed_tools", DISALLOWED_TOOLS)
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
        self.profile_path = (
            Path(profile_path) if profile_path is not None and str(profile_path) else None
        )
        self.memory_enabled = memory_enabled
        self.memory_root = (
            Path(memory_root) if memory_root is not None and str(memory_root) else None
        )
        self.memory_top_k = int(memory_top_k) if memory_top_k is not None else None
        self.memory_budget_tokens = (
            int(memory_budget_tokens) if memory_budget_tokens is not None else None
        )
        self.memory_config = memory_config
        self.memory_task_text = memory_task_text
        self.memory_task_id = str(memory_task_id or "")
        self.code_map = str(code_map or "")
        self.operator_config_path = (
            Path(operator_config_path)
            if operator_config_path is not None and str(operator_config_path)
            else None
        )

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
    profile_sha256: str = ""
    profile_version: int | None = None
    profile_file: str = ""
    init_model: str = ""
    init_digest: str = ""
    memory_ids: list[str] = field(default_factory=list)
    memory_hashes: dict[str, str] = field(default_factory=dict)
    memory_tokens: int = 0
    memory_enabled: bool = True

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
    args: list[str] = ["--permission-mode", "dontAsk"]
    if allowed_tools:
        args.extend(["--allowedTools", *allowed_tools])
    if disallowed_tools:
        args.extend(["--disallowedTools", *disallowed_tools])
    # Tool availability also constrains project settings that grant broader permissions.
    names = list(dict.fromkeys(tool.split("(", 1)[0] for tool in allowed_tools))
    if set(names) != set(ALLOWED_TOOLS):
        args.extend(["--tools", ",".join(names)])
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


MOUNT_SOURCE_FORBIDDEN_CHARS: tuple[str, ...] = (":", ",", "\n", "\r")


def _validate_mount_source(path: Path | str) -> str:
    """Return ``path`` as a docker ``-v`` source string, refusing ``:``, ``,`` and newlines.

    Docker parses ``-v`` values on ``:``; a separator inside a host path would let the
    path be re-interpreted as a different source or destination.
    """
    text = str(path)
    if any(ch in text for ch in MOUNT_SOURCE_FORBIDDEN_CHARS):
        raise HarnessError(
            f"Refusing docker mount source containing ':', ',' or a newline: {text!r}"
        )
    return text


def _parse_worktree_gitdir(dot_git_file: Path, workdir: Path) -> Path | None:
    """Resolve the ``gitdir:`` target from a worktree ``.git`` pointer file (realpath)."""
    text = dot_git_file.read_text(encoding="utf-8", errors="replace")
    raw_gitdir = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("gitdir:"):
            raw_gitdir = stripped.split(":", 1)[1].strip()
            break
    if not raw_gitdir:
        raise HarnessError(f"Malformed .git pointer file: {dot_git_file}")
    gd_path = Path(raw_gitdir)
    return Path(os.path.realpath(workdir / gd_path if not gd_path.is_absolute() else gd_path))


def _trusted_git_common_dir(workdir: Path) -> Path:
    """Ask git (hardened, host side) for the common git dir of the checkout at ``workdir``."""
    try:
        proc = run_git(
            ["rev-parse", "--git-common-dir"],
            repo_root=workdir,
            timeout=_GIT_COMMON_DIR_TIMEOUT_S,
        )
    except (GitSecurityError, OSError, subprocess.SubprocessError) as exc:
        raise HarnessError(f"Cannot verify worktree git metadata in {workdir}: {exc}") from exc
    out = proc.stdout.strip() if proc.returncode == 0 else ""
    if not out:
        raise HarnessError(f"git rev-parse --git-common-dir failed for workdir: {workdir}")
    return Path(os.path.realpath(workdir / out))


def _resolve_worktree_mount_dirs(dot_git_file: Path, resolved_workdir: Path) -> tuple[Path, Path]:
    """Return ``(gitdir, common_dir)`` for a worktree pointer, or refuse with ``HarnessError``.

    Accepted only when ``gitdir`` is a directory directly under ``<common_dir>/worktrees/``,
    contains ``commondir`` and ``HEAD``, and its ``commondir`` file resolves to ``common_dir``.
    ``common_dir`` comes from git run on the host, not from the pointer file alone.
    """
    gitdir = _parse_worktree_gitdir(dot_git_file, resolved_workdir)
    if gitdir is None or not gitdir.is_dir():
        raise HarnessError(f"Worktree gitdir is not a directory: {gitdir}")
    common_dir = _trusted_git_common_dir(resolved_workdir)
    if gitdir.parent != common_dir / "worktrees":
        raise HarnessError(
            f"Refusing worktree gitdir outside '{common_dir / 'worktrees'}': {gitdir}"
        )
    commondir_file = gitdir / "commondir"
    if not commondir_file.is_file() or not (gitdir / "HEAD").is_file():
        raise HarnessError(f"Worktree gitdir lacks commondir or HEAD: {gitdir}")
    declared = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
    if not declared or Path(os.path.realpath(gitdir / declared)) != common_dir:
        raise HarnessError(f"Worktree commondir does not match git common dir: {gitdir}")
    return gitdir, common_dir


def _docker_git_ro_mounts(resolved_workdir: Path) -> list[str]:
    """Return read-only ``-v`` mount flags for ``.git`` in Docker mode (T24 policy).

    * ``.git`` symlink: refused (``HarnessError``), never followed.
    * ``.git`` real directory: mounted read-only at the task ``.git`` path.
    * ``.git`` pointer file: the worktree gitdir and the common dir are mounted read-only
      at their own host paths, after ``_resolve_worktree_mount_dirs`` accepts them.
    """
    git_entry = resolved_workdir / ".git"
    if git_entry.is_symlink():
        raise HarnessError(f"Refusing symlinked .git in workdir: {git_entry}")
    if not git_entry.exists():
        return []
    if git_entry.is_dir():
        src = _validate_mount_source(git_entry)
        return ["-v", f"{src}:{DOCKER_TASK_DIR}/.git:ro"]
    if not git_entry.is_file():
        raise HarnessError(f"Unsupported .git entry type in workdir: {git_entry}")

    gitdir, common_dir = _resolve_worktree_mount_dirs(git_entry, resolved_workdir)
    mounts = ["-v", f"{_validate_mount_source(git_entry)}:{DOCKER_TASK_DIR}/.git:ro"]
    for src in (gitdir, common_dir):
        safe_src = _validate_mount_source(src)
        mounts.extend(["-v", f"{safe_src}:{safe_src}:ro"])
    return mounts


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
            command = format_command(
                engine.cmd,
                prompt=prompt,
                prompt_file=prompt_file,
                role_model=engine.model,
            )
            if command and Path(command[0]).name in ("claude", "claude.exe"):
                permission_flags = {
                    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                    "--permission-mode", "--permission-prompts", "--tools",
                    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                }
                if any(part.split("=", 1)[0] in permission_flags for part in engine.cmd):
                    raise HarnessError("Configure tool permissions in the role table, not the Claude command")
                command[1:1] = build_permission_args(allowed_tools, disallowed_tools)
            return command
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
        f"{_validate_mount_source(resolved_workdir)}:{DOCKER_TASK_DIR}:rw",
        "-v",
        f"{_validate_mount_source(resolved_cfg)}:/workspace/cfg:ro",
        *_docker_git_ro_mounts(resolved_workdir),
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


# Redaction helpers (json_string_leaves, collect_os_identities, redact_os_identities,
# redact_sensitive_text, redact_value, redact_structure, redact_stream_line) live in runner/redaction.py.


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
    init_model = ""
    init_digest = ""
    summed_in_tokens = 0
    summed_out_tokens = 0
    permission_denials: list[Any] = []

    result_event: dict[str, Any] | None = None

    # Split on "\n" only: str.splitlines() also breaks on U+2028, U+0085 and other separators
    # that may appear unescaped inside JSON strings.
    for raw_line in raw_text.split("\n"):
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
                init_model = event["model"]
            for dkey in ("model_digest", "digest"):
                if isinstance(event.get(dkey), str) and event[dkey]:
                    init_digest = event[dkey]
                    break

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
            init_model=init_model,
            init_digest=init_digest,
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
        init_model=init_model,
        init_digest=init_digest,
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

    profile_body, profile_sha256, profile_version, profile_file = load_engine_profile(
        request.profile_path
    )
    effective_prompt = request.prompt
    if request.code_map and "## Code map" not in effective_prompt:
        effective_prompt = (
            effective_prompt.rstrip()
            + "\n\n## Code map\n\n"
            + request.code_map.strip()
            + "\n"
        )
    if profile_body and "## Known mistakes for this engine" not in effective_prompt:
        effective_prompt = (
            effective_prompt.rstrip()
            + "\n\n## Known mistakes for this engine\n"
            + profile_body
            + "\n"
        )

    mem_cfg = request.memory_config if request.memory_config is not None else MemoryConfig()
    effective_mem_enabled = (
        bool(request.memory_enabled)
        if request.memory_enabled is not None
        else bool(mem_cfg.enabled)
    )
    effective_top_k = (
        request.memory_top_k if request.memory_top_k is not None else int(mem_cfg.top_k)
    )
    effective_budget_tokens = (
        request.memory_budget_tokens
        if request.memory_budget_tokens is not None
        else int(mem_cfg.budget_tokens)
    )
    mem_root = (
        request.memory_root
        if request.memory_root is not None
        else devpack_memory.memory_root_for(request.workdir)
    )
    memory_ids: list[str] = []
    memory_hashes: dict[str, str] = {}
    memory_tokens = 0
    if (
        effective_mem_enabled
        and mem_root.is_dir()
        and devpack_memory.INJECTED_MEMORY_HEADING not in effective_prompt
        and "## Relevant project memory" not in effective_prompt
    ):
        q_res = devpack_memory.retrieve_memory(
            mem_root,
            request.memory_task_text or request.prompt,
            role=request.role,
            engine=request.engine.name,
            top_k=effective_top_k,
            budget_tokens=effective_budget_tokens,
            reviewer_sees_handoffs=bool(mem_cfg.reviewer_sees_handoffs),
            role_scopes=mem_cfg.role_scopes,
            task_id=request.memory_task_id or None,
        )
        if q_res.formatted_prompt:
            effective_prompt = (
                effective_prompt.rstrip() + "\n\n" + q_res.formatted_prompt.rstrip() + "\n"
            )
            memory_ids = list(q_res.ids)
            memory_hashes = dict(q_res.hashes)
            memory_tokens = int(q_res.total_tokens)

    effective_request = copy.copy(request)
    effective_request.prompt = effective_prompt
    prompt_sha256 = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()

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

    os_identities = collect_os_identities()

    def _redact(text: str) -> str:
        """Redact secrets, home paths, and OS identities using request settings."""
        return redact_sensitive_text(
            text,
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
            identities=os_identities,
            credentials_path=cred_path,
        )

    prompt_path.write_text(_redact(effective_prompt), encoding="utf-8")

    def _write_meta_and_return(res: AgentResult) -> AgentResult:
        """Populate metadata fields, write .meta.json, and return the result."""
        res.transcript_path = jsonl_path
        res.model = res.model or request.engine.model
        res.engine = request.engine.name
        res.role = request.role
        res.prompt_sha256 = prompt_sha256
        res.prompt_file = prompt_path.name
        res.profile_sha256 = profile_sha256
        res.profile_version = profile_version
        res.profile_file = profile_file
        res.memory_ids = list(memory_ids)
        res.memory_hashes = dict(memory_hashes)
        res.memory_tokens = memory_tokens
        res.memory_enabled = effective_mem_enabled
        res.final_text = _redact(res.final_text)
        res.error = _redact(res.error)
        res.stderr = _redact(res.stderr)
        res.permission_denials = redact_value(
            res.permission_denials,
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
            identities=os_identities,
            credentials_path=cred_path,
        )
        meta_payload = redact_value(
            res.to_dict(),
            extra_secrets=secrets_list,
            path_replacements=request.path_replacements,
            home_placeholder=request.home_placeholder,
            identities=os_identities,
            credentials_path=cred_path,
        )
        meta_path.write_text(json.dumps(meta_payload, indent=2) + "\n", encoding="utf-8")
        return res

    iso_allowed, iso_msg = check_local_isolation_allowed(
        request.isolation,
        allow_unsandboxed_local=request.allow_unsandboxed_local,
        workdir=request.workdir,
        operator_config_path=request.operator_config_path,
    )
    ignored_opt_out = WORKDIR_SANDBOX_OPT_OUT_WARNING in iso_msg
    if not iso_allowed:
        sys.stderr.write(iso_msg + "\n")
        jsonl_path.write_text("", encoding="utf-8")
        refused_updates: dict[str, Any] = {
            "isolation": request.isolation,
            "outcome": "refused",
        }
        if ignored_opt_out:
            refused_updates["sandbox_warning"] = iso_msg
            refused_updates["ignored_workdir_sandbox_opt_out"] = True
        update_run_json(request.run_dir, **refused_updates)
        return _write_meta_and_return(
            AgentResult(
                status="harness_error",
                exit_code=126,
                error=iso_msg,
            )
        )
    if iso_msg:
        sys.stderr.write(iso_msg + "\n")
        warn_updates: dict[str, Any] = {
            "isolation": request.isolation,
            "sandbox_warning": iso_msg,
        }
        if ignored_opt_out:
            warn_updates["ignored_workdir_sandbox_opt_out"] = True
        update_run_json(request.run_dir, **warn_updates)

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
                effective_request,
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
                        redacted_line = redact_stream_line(
                            raw_line,
                            extra_secrets=secrets_list,
                            path_replacements=request.path_replacements,
                            home_placeholder=request.home_placeholder,
                            identities=os_identities,
                            credentials_path=cred_path,
                        )
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


def install_project_skills(workdir: Path) -> list[str]:
    """Copy bundled skills into a project's local skill directory before its task baseline."""
    root = workdir.resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory not found: {workdir}")
    skills_root = root / ".claude" / "skills"
    if not skills_root.resolve().is_relative_to(root):
        raise ValueError("Skills installation path escapes the project")
    source = Path(__file__).resolve().parents[1] / "knowledge" / "skills"
    installed: list[str] = []
    for skill in sorted(source.iterdir()):
        if not (skill / "SKILL.md").is_file():
            continue
        destination = skills_root / skill.name
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError(f"Unsafe existing skill path: {destination}")
            # Existing installations are project-owned; do not overwrite edits.
            continue
        shutil.copytree(skill, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        installed.append(skill.name)
    return installed


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/agent_exec.py``."""
    parser = argparse.ArgumentParser(
        prog="agent_exec.py",
        description=(
            "Single isolated agent executor, v2/v0.1 engine configuration loader, "
            "and stream-json transcript parser."
        ),
    )
    parser.add_argument("--install-skills", default="", metavar="PROJECT", help="Copy bundled skills into PROJECT/.claude/skills before committing the task baseline")
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

    if args.install_skills:
        try:
            installed = install_project_skills(Path(args.install_skills))
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("Installed skills: " + ", ".join(installed))
        return 0

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
