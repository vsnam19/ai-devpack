#!/usr/bin/env python3
# Header: Human-readable agent log renderer, viewer, lister, and static site generator.
# Renders run directories (runs/<run-id>/) and interactive Claude Code session JSONL
# files into self-contained Markdown (transcript.md) and static HTML (index.html),
# lists runs with cost/duration totals, shows steps in the terminal, and builds a
# static index page across all runs. Applies secret/PII redaction via
# enforcement/checks/secret_pii_check.py, env variable redaction, credential string
# scrubbing, and home directory normalization (-> ~).
#
# Exit codes:
#   0 - Command succeeded.
#   1 - Runtime error (missing directory/file, unreadable JSON, step not found).
#   2 - Invalid command-line arguments or usage error.
"""Render, inspect, list, and index human-readable agent logs in Markdown and static HTML."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import html
import json
import os
import pathlib
import re
import sys
from typing import Any, Sequence

RUNNER_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = RUNNER_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from enforcement.checks import secret_pii_check  # noqa: E402

try:
    from runner.agent_exec import collect_os_identities, redact_os_identities, redact_value  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - direct runner/ invocation
    from agent_exec import collect_os_identities, redact_os_identities, redact_value  # type: ignore[no-redef]  # noqa: E402

TRUNCATE_LIMIT_BYTES = 4096

# Construct home prefix dynamically so literal forbidden pattern never appears in source.
_LINUX_HOME_PREFIX = "/" + "ho" + "me"
_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:" + _LINUX_HOME_PREFIX + r"|/Users)/[A-Za-z0-9_.-]+"
)

# Private key block matcher (multi-line) for complete block redaction.
_BEGIN_WORD = "-----BEGIN"
_END_WORD = "-----END"
_PRIV_KEY_SUFFIX = "PRIVATE" + " KEY-----"
_PRIVATE_KEY_BLOCK_RE = re.compile(
    rf"{_BEGIN_WORD}\s+(?:[A-Z0-9 ]+\s+)?{_PRIV_KEY_SUFFIX}[\s\S]*?{_END_WORD}\s+(?:[A-Z0-9 ]+\s+)?{_PRIV_KEY_SUFFIX}"
)

# Credential-looking token patterns.
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_PREFIXED_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-ant-[A-Za-z0-9_-]{8,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{16,}"
    r"|gho_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|glpat-[A-Za-z0-9_-]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r")\b"
)
_CRED_ASSIGN_RE = re.compile(
    r"(?i)\b((?:[a-z0-9_]*_)?(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|secret[_-]?key|private[_-]?key|bearer[_-]?token|secret|token))"
    r"(\s*[:=]\s*['\"]?)(?![0-9]+\b)([A-Za-z0-9._~+/=-]{8,})(['\"]?)"
)
_JSON_CRED_RE = re.compile(
    r"(?i)(['\"](?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|secret[_-]?key|private[_-]?key|sessionKey|claudeAiOauth"
    r"|password|passwd|secret|token)['\"]\s*:\s*['\"])([^'\"\s]{6,})(['\"])"
)

_DEFAULT_AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEVPACK_LOCAL_AUTH_TOKEN",
)

_STEP_PREFIX_RE = re.compile(r"^(\d+)[-_](.+)$")
_KNOWN_EVENT_TYPES = {"system", "assistant", "user", "result"}
_KNOWN_SYSTEM_SUBTYPES = {"init", "thinking_tokens"}

_VERDICT_PATTERNS = (
    re.compile(r"^\s*(VERDICT:\s*(?:APPROVE|REQUEST_CHANGES))\s*$", re.MULTILINE),
    re.compile(r"^\s*(ESCALATE_TO_HUMAN(?::.*)?)\s*$", re.MULTILINE),
    re.compile(r"^\s*(GATE_OK|GATE_FAIL(?:\s+\S+)?)\s*$", re.MULTILINE),
)


@dataclass
class StreamItem:
    """Single chronological event item extracted from an agent stream-json file."""

    kind: str  # "init" | "thinking" | "text" | "tool_use" | "tool_result" | "user_text"
    text: str = ""
    tool_name: str = ""
    tool_use_id: str = ""
    tool_input_json: str = ""
    is_error: bool = False
    is_protocol_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepRecord:
    """Parsed and normalized representation of a single step in a run directory."""

    step_num: str
    step_id: str
    role: str
    engine: str
    model: str = "-"
    status: str = "ok"
    duration_s: float = 0.0
    num_turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    prompt_text: str = ""
    prompt_sha256: str = ""
    is_gate: bool = False
    gate_text: str = ""
    items: list[StreamItem] = field(default_factory=list)
    final_result: str = ""
    verdict_line: str = ""
    unknown_events: int = 0
    tool_protocol_errors: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RunRecord:
    """Parsed and normalized representation of an entire run directory or session."""

    run_id: str
    run_dir: pathlib.Path
    kind: str = "task"
    started: str = "-"
    finished: str = "-"
    engines_label: str = "-"
    task_label: str = "-"
    outcome: str = "-"
    cost_usd: float = 0.0
    duration_s: float = 0.0
    total_turns: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_unknown_events: int = 0
    total_protocol_errors: int = 0
    verdict_line: str = ""
    steps: list[StepRecord] = field(default_factory=list)


class Redactor:
    """Redacts secrets, PII patterns, environment token values, credentials, and home paths."""

    def __init__(
        self,
        redact_env_names: Sequence[str] | None = None,
        patterns_file: pathlib.Path | str | None = None,
        extra_literals: Sequence[str] | None = None,
    ) -> None:
        """Initialize redaction rules from secret_pii_check, environment variables, and patterns."""
        self.rules: list[tuple[str, re.Pattern[str]]] = list(
            secret_pii_check.get_builtin_rules()
        )

        resolved_patterns = patterns_file or os.environ.get("DEVPACK_SECRET_PATTERNS")
        if resolved_patterns:
            p_path = pathlib.Path(resolved_patterns)
            if p_path.is_file():
                custom_rules, _ = secret_pii_check.load_external_patterns(p_path)
                if custom_rules:
                    self.rules.extend(custom_rules)

        literals: list[str] = []
        env_names: list[str] = list(_DEFAULT_AUTH_ENV_NAMES)
        if redact_env_names:
            for item in redact_env_names:
                for part in item.split(","):
                    cleaned = part.strip()
                    if cleaned and cleaned not in env_names:
                        env_names.append(cleaned)

        for env_name in env_names:
            val = os.environ.get(env_name, "")
            if val and val.strip():
                literals.append(val.strip())
                if val != val.strip():
                    literals.append(val)

        cred_file = os.environ.get("DEVPACK_CLAUDE_CREDENTIALS", "")
        if cred_file:
            cred_path = pathlib.Path(cred_file)
            if cred_path.is_file():
                try:
                    raw_cred = cred_path.read_text(encoding="utf-8", errors="replace")
                    literals.extend(_extract_credential_file_secrets(raw_cred))
                except OSError:
                    pass

        if extra_literals:
            for item in extra_literals:
                if item and item.strip():
                    literals.append(item.strip())

        # Sort longer literals first to avoid partial prefix replacements.
        self.secret_literals: list[str] = sorted(
            {lit for lit in literals if len(lit) >= 4},
            key=len,
            reverse=True,
        )

        current_home = os.path.expanduser("~")
        self.current_home: str | None = (
            current_home.rstrip("/")
            if current_home and current_home not in ("~", "/")
            else None
        )
        self._os_identities = collect_os_identities()

    def redact(self, text: str) -> str:
        """Return a redacted copy of ``text`` with secrets, home paths, and OS IDs scrubbed."""
        if not text:
            return text

        out = text

        for secret_val in self.secret_literals:
            if secret_val in out:
                out = out.replace(secret_val, "[REDACTED]")

        out = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED]", out)

        for _, rx in self.rules:
            out = rx.sub("[REDACTED]", out)

        out = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", out)
        out = _PREFIXED_TOKEN_RE.sub("[REDACTED]", out)
        out = _JSON_CRED_RE.sub(r"\1[REDACTED]\3", out)
        out = _CRED_ASSIGN_RE.sub(r"\1\2[REDACTED]\4", out)

        if self.current_home and self.current_home in out:
            out = out.replace(self.current_home, "~")
        out = _HOME_PATH_RE.sub("~", out)
        out = redact_os_identities(out, self._os_identities)

        return out

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings and dict keys in ``value`` with no exemptions."""
        return redact_value(value, _text_fn=self.redact)


def _extract_credential_file_secrets(raw_text: str) -> list[str]:
    """Extract secret string values from a JSON or plain-text credential file."""
    found: list[str] = []
    stripped = raw_text.strip()
    if not stripped:
        return found
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            clean = line.strip()
            if len(clean) >= 8:
                found.append(clean)
        return found

    def _walk(node: Any) -> None:
        """Recursively collect string leaves that look like tokens or secrets."""
        if isinstance(node, dict):
            for k, v in node.items():
                k_low = str(k).lower()
                if isinstance(v, str) and len(v) >= 8 and any(
                    word in k_low for word in ("token", "key", "secret", "password", "auth")
                ):
                    found.append(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for elem in node:
                _walk(elem)

    _walk(parsed)
    return found


def redact_text(
    text: str,
    redact_env_names: Sequence[str] | None = None,
    patterns_file: pathlib.Path | str | None = None,
    extra_literals: Sequence[str] | None = None,
) -> str:
    """Convenience function to redact a string using ``Redactor``."""
    redactor = Redactor(
        redact_env_names=redact_env_names,
        patterns_file=patterns_file,
        extra_literals=extra_literals,
    )
    return redactor.redact(text)


def truncate_text(text: str, full: bool = False, limit: int = TRUNCATE_LIMIT_BYTES) -> str:
    """Truncate ``text`` to ``limit`` characters unless ``full`` is True."""
    if full or len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n... [truncated at 4 KB ({len(text)} chars total); use --full for full output]"
    )


def _looks_like_json_tool_call(text: str) -> bool:
    """Return True if assistant ``text`` contains a JSON object shaped like a tool call."""
    if not text or "{" not in text or "}" not in text:
        return False

    candidates: list[str] = []
    fence_re = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
    for match in fence_re.finditer(text):
        candidates.append(match.group(1))

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            has_name = isinstance(obj.get("name"), str) or isinstance(obj.get("tool"), str)
            has_args = any(k in obj for k in ("arguments", "input", "parameters", "args"))
            if has_name and has_args:
                return True
    return False


def _normalize_tool_result_content(raw_content: Any) -> str:
    """Convert a ``tool_result`` content payload (str, list of blocks, dict, etc.) to str."""
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        parts: list[str] = []
        for elem in raw_content:
            if isinstance(elem, dict):
                if isinstance(elem.get("text"), str):
                    parts.append(elem["text"])
                else:
                    parts.append(json.dumps(elem, ensure_ascii=False))
            else:
                parts.append(str(elem))
        return "\n".join(parts)
    if isinstance(raw_content, dict):
        return json.dumps(raw_content, indent=2, ensure_ascii=False)
    return str(raw_content)


def _extract_verdict_line(text: str) -> str:
    """Extract the last highlighted verdict or gate status line from ``text``, if present."""
    if not text:
        return ""
    for rx in _VERDICT_PATTERNS:
        matches = rx.findall(text)
        if matches:
            return str(matches[-1]).strip()
    return ""


def parse_stream_jsonl(
    jsonl_path: pathlib.Path,
    redactor: Redactor,
    step: StepRecord,
) -> None:
    """Parse a stream-json or session ``.jsonl`` file into ``step`` with redaction applied."""
    try:
        # Split on "\n" only: splitlines() would also break on U+2028, U+0085 and similar characters.
        raw_lines = jsonl_path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        step.status = "harness_error"
        return

    tool_id_to_name: dict[str, str] = {}
    saw_tool_use = False
    assistant_turns = 0
    sum_in_tokens = 0
    sum_out_tokens = 0
    sum_cache_read = 0
    sum_cache_create = 0
    saw_result_usage = False
    protocol_errors_detected = 0
    text_items_with_json_call: list[StreamItem] = []

    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            step.unknown_events += 1
            continue

        if not isinstance(event, dict):
            step.unknown_events += 1
            continue

        ev_type = event.get("type")
        if not isinstance(ev_type, str) or ev_type not in _KNOWN_EVENT_TYPES:
            step.unknown_events += 1
            continue

        if ev_type == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                model_val = event.get("model")
                if isinstance(model_val, str) and model_val and step.model in ("-", ""):
                    step.model = redactor.redact(model_val)
                cwd_val = redactor.redact(str(event.get("cwd", "")))
                tools_raw = event.get("tools", [])
                tools_list = (
                    [redactor.redact(str(t)) for t in tools_raw]
                    if isinstance(tools_raw, list)
                    else []
                )
                step.items.append(
                    StreamItem(
                        kind="init",
                        text=f"model={step.model} cwd={cwd_val}",
                        metadata={"model": step.model, "cwd": cwd_val, "tools": tools_list},
                    )
                )
            elif subtype in _KNOWN_SYSTEM_SUBTYPES:
                # Stream progress telemetry such as thinking_tokens; ignore silently.
                continue
            else:
                step.unknown_events += 1
            continue

        if ev_type == "assistant":
            assistant_turns += 1
            msg = event.get("message")
            if not isinstance(msg, dict):
                continue
            msg_model = msg.get("model")
            if isinstance(msg_model, str) and msg_model and step.model in ("-", ""):
                step.model = redactor.redact(msg_model)

            usage = msg.get("usage")
            if isinstance(usage, dict):
                sum_in_tokens += int(usage.get("input_tokens", 0) or 0)
                sum_out_tokens += int(usage.get("output_tokens", 0) or 0)
                sum_cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
                sum_cache_create += int(usage.get("cache_creation_input_tokens", 0) or 0)

            content = msg.get("content")
            if isinstance(content, str):
                redacted_txt = redactor.redact(content)
                item = StreamItem(kind="text", text=redacted_txt)
                if _looks_like_json_tool_call(redacted_txt):
                    text_items_with_json_call.append(item)
                step.items.append(item)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    b_type = block.get("type")
                    if b_type == "thinking":
                        think_txt = redactor.redact(str(block.get("thinking", "")))
                        if think_txt:
                            step.items.append(StreamItem(kind="thinking", text=think_txt))
                    elif b_type == "text":
                        txt = redactor.redact(str(block.get("text", "")))
                        if txt:
                            item = StreamItem(kind="text", text=txt)
                            if _looks_like_json_tool_call(txt):
                                text_items_with_json_call.append(item)
                            step.items.append(item)
                    elif b_type == "tool_use":
                        saw_tool_use = True
                        t_name = redactor.redact(str(block.get("name", "unknown_tool")))
                        t_id = redactor.redact(str(block.get("id", "")))
                        t_input = redactor.redact_value(block.get("input", {}))
                        try:
                            pretty_in = json.dumps(t_input, indent=2, ensure_ascii=False)
                        except (TypeError, ValueError):
                            pretty_in = str(t_input)
                        pretty_in = redactor.redact(pretty_in)
                        if t_id:
                            tool_id_to_name[t_id] = t_name
                        step.tool_counts[t_name] = step.tool_counts.get(t_name, 0) + 1
                        step.items.append(
                            StreamItem(
                                kind="tool_use",
                                tool_name=t_name,
                                tool_use_id=t_id,
                                tool_input_json=pretty_in,
                            )
                        )
                    else:
                        step.unknown_events += 1
            continue

        if ev_type == "user":
            msg = event.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                redacted_user = redactor.redact(content)
                if not step.prompt_text:
                    step.prompt_text = redacted_user
                else:
                    step.items.append(StreamItem(kind="user_text", text=redacted_user))
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    b_type = block.get("type")
                    if b_type == "tool_result":
                        t_id = redactor.redact(str(block.get("tool_use_id", "")))
                        t_name = tool_id_to_name.get(t_id, "")
                        raw_res = _normalize_tool_result_content(
                            redactor.redact_value(block.get("content"))
                        )
                        redacted_res = redactor.redact(raw_res)
                        is_err = bool(block.get("is_error", False))
                        step.items.append(
                            StreamItem(
                                kind="tool_result",
                                tool_name=t_name,
                                tool_use_id=t_id,
                                text=redacted_res,
                                is_error=is_err,
                            )
                        )
                    elif b_type == "text":
                        redacted_user = redactor.redact(str(block.get("text", "")))
                        if redacted_user:
                            if not step.prompt_text:
                                step.prompt_text = redacted_user
                            else:
                                step.items.append(StreamItem(kind="user_text", text=redacted_user))
                    else:
                        step.unknown_events += 1
            continue

        if ev_type == "result":
            res_text = event.get("result")
            if isinstance(res_text, str) and res_text:
                step.final_result = redactor.redact(res_text)
            cost_val = event.get("total_cost_usd")
            if isinstance(cost_val, (int, float)) and step.cost_usd == 0.0:
                step.cost_usd = float(cost_val)
            turns_val = event.get("num_turns")
            if isinstance(turns_val, int) and step.num_turns == 0:
                step.num_turns = turns_val
            dur_ms = event.get("duration_ms")
            if isinstance(dur_ms, (int, float)) and step.duration_s == 0.0:
                step.duration_s = round(float(dur_ms) / 1000.0, 3)

            is_err = bool(event.get("is_error", False))
            subtype = str(event.get("subtype", "success"))
            if step.status == "ok" and (is_err or subtype not in ("success", "ok", "")):
                step.status = "agent_error"

            usage = event.get("usage")
            if isinstance(usage, dict):
                saw_result_usage = True
                if step.tokens_in == 0:
                    step.tokens_in = int(usage.get("input_tokens", 0) or 0)
                if step.tokens_out == 0:
                    step.tokens_out = int(usage.get("output_tokens", 0) or 0)
                if step.cache_read_tokens == 0:
                    step.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
                if step.cache_creation_tokens == 0:
                    step.cache_creation_tokens = int(
                        usage.get("cache_creation_input_tokens", 0) or 0
                    )

            model_usage = event.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage and step.model in ("-", ""):
                first_model = next(iter(model_usage.keys()))
                step.model = redactor.redact(str(first_model))

    if not saw_tool_use and text_items_with_json_call:
        for item in text_items_with_json_call:
            item.is_protocol_error = True
            protocol_errors_detected += 1

    if step.tool_protocol_errors < protocol_errors_detected:
        step.tool_protocol_errors = protocol_errors_detected

    if step.num_turns == 0 and assistant_turns > 0:
        step.num_turns = assistant_turns

    if not saw_result_usage:
        if step.tokens_in == 0:
            step.tokens_in = sum_in_tokens
        if step.tokens_out == 0:
            step.tokens_out = sum_out_tokens
        if step.cache_read_tokens == 0:
            step.cache_read_tokens = sum_cache_read
        if step.cache_creation_tokens == 0:
            step.cache_creation_tokens = sum_cache_create

    if not step.final_result:
        for item in reversed(step.items):
            if item.kind == "text" and item.text:
                step.final_result = item.text
                break

    verdict = _extract_verdict_line(step.final_result)
    if not verdict:
        for item in reversed(step.items):
            if item.kind == "text":
                verdict = _extract_verdict_line(item.text)
                if verdict:
                    break
    if verdict:
        step.verdict_line = verdict


def _apply_step_meta(step: StepRecord, meta: dict[str, Any], redactor: Redactor) -> None:
    """Populate ``step`` fields from a ``.meta.json`` dictionary."""
    if isinstance(meta.get("role"), str) and meta["role"]:
        step.role = redactor.redact(meta["role"])
    if isinstance(meta.get("engine"), str) and meta["engine"]:
        step.engine = redactor.redact(meta["engine"])
    if isinstance(meta.get("model"), str) and meta["model"]:
        step.model = redactor.redact(meta["model"])
    if isinstance(meta.get("status"), str) and meta["status"]:
        step.status = redactor.redact(meta["status"])
    if isinstance(meta.get("duration_s"), (int, float)):
        step.duration_s = float(meta["duration_s"])
    elif isinstance(meta.get("duration_ms"), (int, float)):
        step.duration_s = round(float(meta["duration_ms"]) / 1000.0, 3)

    if isinstance(meta.get("num_turns"), int):
        step.num_turns = int(meta["num_turns"])
    elif isinstance(meta.get("turns"), int):
        step.num_turns = int(meta["turns"])

    for in_key in ("tokens_in", "input_tokens", "in_tokens"):
        if isinstance(meta.get(in_key), int):
            step.tokens_in = int(meta[in_key])
            break
    for out_key in ("tokens_out", "output_tokens", "out_tokens"):
        if isinstance(meta.get(out_key), int):
            step.tokens_out = int(meta[out_key])
            break

    tokens_dict = meta.get("tokens")
    if isinstance(tokens_dict, dict):
        if isinstance(tokens_dict.get("in"), int) and step.tokens_in == 0:
            step.tokens_in = int(tokens_dict["in"])
        if isinstance(tokens_dict.get("out"), int) and step.tokens_out == 0:
            step.tokens_out = int(tokens_dict["out"])

    for cost_key in ("cost_usd", "total_cost_usd", "cost"):
        if isinstance(meta.get(cost_key), (int, float)):
            step.cost_usd = float(meta[cost_key])
            break

    if isinstance(meta.get("final_text"), str) and meta["final_text"]:
        step.final_result = redactor.redact(meta["final_text"])

    tpe = meta.get("tool_protocol_errors")
    if isinstance(tpe, int):
        step.tool_protocol_errors = max(step.tool_protocol_errors, tpe)
    elif isinstance(tpe, list):
        step.tool_protocol_errors = max(step.tool_protocol_errors, len(tpe))

    for sha_key in ("prompt_sha256", "prompt_hash"):
        if isinstance(meta.get(sha_key), str) and meta[sha_key]:
            step.prompt_sha256 = redactor.redact(meta[sha_key])
            break


def _parse_step_stem(stem: str) -> tuple[str, str, str]:
    """Split a step filename stem like ``01-implementer-haiku`` into ``(num, role, engine)``."""
    match = _STEP_PREFIX_RE.match(stem)
    if not match:
        return ("00", stem, "-")
    step_num = match.group(1)
    rest = match.group(2)
    if rest == "gate" or rest.startswith("gate-"):
        return (step_num, "gate", "-")
    parts = rest.split("-", 1)
    if len(parts) == 2:
        return (step_num, parts[0], parts[1])
    return (step_num, rest, "-")


def _format_engines_field(raw_engines: Any) -> str:
    """Format the ``engines`` field from ``run.json`` into a concise string."""
    if isinstance(raw_engines, str) and raw_engines.strip():
        return raw_engines.strip()
    if isinstance(raw_engines, list) and raw_engines:
        return ",".join(str(x) for x in raw_engines)
    if isinstance(raw_engines, dict) and raw_engines:
        vals: list[str] = []
        for k, v in raw_engines.items():
            vals.append(f"{k}:{v}")
        return ",".join(vals)
    return "-"


def _format_task_field(raw_task: Any, fallback_meta: dict[str, Any]) -> str:
    """Format the ``task`` field from ``run.json`` into a concise string."""
    if isinstance(raw_task, str) and raw_task.strip():
        return raw_task.strip()
    if isinstance(raw_task, dict):
        tid = raw_task.get("id") or raw_task.get("task_id") or raw_task.get("name")
        title = raw_task.get("title")
        if tid and title:
            return f"{tid} ({title})"
        if tid:
            return str(tid)
        if title:
            return str(title)
    for key in ("task_id", "task_title", "title"):
        if isinstance(fallback_meta.get(key), str) and fallback_meta[key].strip():
            return str(fallback_meta[key]).strip()
    return "-"


def _compute_iso_duration_s(started: str, finished: str) -> float:
    """Compute elapsed seconds between two ISO-8601 timestamps, or 0.0 on parse failure."""
    if not started or not finished or started == "-" or finished == "-":
        return 0.0
    try:
        s_clean = started.replace("Z", "+00:00")
        f_clean = finished.replace("Z", "+00:00")
        dt_start = datetime.fromisoformat(s_clean)
        dt_end = datetime.fromisoformat(f_clean)
        delta = (dt_end - dt_start).total_seconds()
        return round(max(0.0, delta), 3)
    except ValueError:
        return 0.0


def load_run_dir(run_dir: pathlib.Path, redactor: Redactor) -> RunRecord:
    """Load and parse a run directory conforming to SPEC_v02 section 2."""
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    run_json_path = run_dir / "run.json"
    run_meta: dict[str, Any] = {}
    if run_json_path.is_file():
        try:
            loaded = json.loads(run_json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                run_meta = loaded
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {run_json_path}: {exc}") from exc

    run_id = redactor.redact(str(run_meta.get("run_id") or run_dir.name))
    kind = redactor.redact(str(run_meta.get("kind") or "task"))
    started = redactor.redact(str(run_meta.get("started") or "-"))
    finished = redactor.redact(str(run_meta.get("finished") or "-"))
    engines_label = redactor.redact(_format_engines_field(run_meta.get("engines")))
    task_label = redactor.redact(_format_task_field(run_meta.get("task"), run_meta))
    outcome_raw = (
        run_meta.get("outcome")
        or run_meta.get("status")
        or run_meta.get("verdict")
        or "-"
    )
    outcome = redactor.redact(str(outcome_raw))

    record = RunRecord(
        run_id=run_id,
        run_dir=run_dir,
        kind=kind,
        started=started,
        finished=finished,
        engines_label=engines_label,
        task_label=task_label,
        outcome=outcome,
    )

    steps_dir = run_dir / "steps"
    step_entries: list[tuple[int, int, str, StepRecord]] = []

    if steps_dir.is_dir():
        # Discover agent steps (.jsonl or .meta.json) and gate steps (NN-gate*.txt).
        agent_stems: set[str] = set()
        gate_files: list[pathlib.Path] = []

        for path in sorted(steps_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(".jsonl"):
                agent_stems.add(name[: -len(".jsonl")])
            elif name.endswith(".meta.json"):
                agent_stems.add(name[: -len(".meta.json")])
            elif name.endswith(".prompt.md"):
                agent_stems.add(name[: -len(".prompt.md")])
            elif name.endswith(".txt") and ("gate" in name or name.endswith("-grader.txt")):
                gate_files.append(path)

        for stem in sorted(agent_stems):
            step_num, role, engine = _parse_step_stem(stem)
            step = StepRecord(
                step_num=step_num,
                step_id=stem,
                role=redactor.redact(role),
                engine=redactor.redact(engine),
            )
            prompt_path = steps_dir / f"{stem}.prompt.md"
            if prompt_path.is_file():
                try:
                    step.prompt_text = redactor.redact(
                        prompt_path.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    pass

            meta_path = steps_dir / f"{stem}.meta.json"
            if meta_path.is_file():
                try:
                    meta_obj = json.loads(
                        meta_path.read_text(encoding="utf-8", errors="replace")
                    )
                    if isinstance(meta_obj, dict):
                        _apply_step_meta(step, meta_obj, redactor)
                except (OSError, json.JSONDecodeError):
                    pass

            jsonl_path = steps_dir / f"{stem}.jsonl"
            if jsonl_path.is_file():
                parse_stream_jsonl(jsonl_path, redactor, step)

            if step.model in ("-", "") and step.engine not in ("-", ""):
                step.model = step.engine

            try:
                num_int = int(step_num)
            except ValueError:
                num_int = 0
            step_entries.append((num_int, 0, stem, step))

        for g_path in sorted(gate_files):
            stem = g_path.stem
            step_num, _, _ = _parse_step_stem(stem)
            try:
                g_text = redactor.redact(
                    g_path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                g_text = ""
            verdict = _extract_verdict_line(g_text)
            g_status = "fail" if "GATE_FAIL" in g_text else "ok"
            step = StepRecord(
                step_num=step_num,
                step_id=stem,
                role="gate",
                engine="-",
                model="-",
                status=g_status,
                is_gate=True,
                gate_text=g_text,
                verdict_line=verdict,
            )
            try:
                num_int = int(step_num)
            except ValueError:
                num_int = 0
            step_entries.append((num_int, 1, stem, step))

    step_entries.sort(key=lambda item: (item[0], item[1], item[2]))
    record.steps = [item[3] for item in step_entries]

    _finalize_run_record(record, run_meta)
    return record


def load_session_jsonl(
    session_path: pathlib.Path,
    out_dir: pathlib.Path,
    redactor: Redactor,
) -> RunRecord:
    """Load a standalone Claude Code session or stream-json ``.jsonl`` file into a ``RunRecord``."""
    if not session_path.is_file():
        raise FileNotFoundError(f"Session file not found: {session_path}")

    stem = redactor.redact(session_path.stem)
    step = StepRecord(
        step_num="01",
        step_id=f"01-session-{stem}",
        role="session",
        engine="claude",
    )
    parse_stream_jsonl(session_path, redactor, step)
    if step.model not in ("-", ""):
        step.engine = step.model

    record = RunRecord(
        run_id=stem,
        run_dir=out_dir,
        kind="session",
        started="-",
        finished="-",
        engines_label=step.model if step.model != "-" else step.engine,
        task_label=redactor.redact(session_path.name),
        outcome=step.status,
        steps=[step],
    )
    _finalize_run_record(record, {})
    return record


def _finalize_run_record(record: RunRecord, run_meta: dict[str, Any]) -> None:
    """Compute aggregate totals, inferred engines, and final verdict on ``record``."""
    sum_cost = sum(s.cost_usd for s in record.steps)
    sum_dur = sum(s.duration_s for s in record.steps)
    record.total_turns = sum(s.num_turns for s in record.steps)
    record.total_tokens_in = sum(s.tokens_in for s in record.steps)
    record.total_tokens_out = sum(s.tokens_out for s in record.steps)
    record.total_unknown_events = sum(s.unknown_events for s in record.steps)
    record.total_protocol_errors = sum(s.tool_protocol_errors for s in record.steps)

    meta_cost = run_meta.get("cost_usd") or run_meta.get("total_cost_usd")
    if isinstance(meta_cost, (int, float)) and float(meta_cost) > 0:
        record.cost_usd = float(meta_cost)
    else:
        record.cost_usd = round(sum_cost, 8)

    meta_dur = run_meta.get("duration_s")
    meta_dur_ms = run_meta.get("duration_ms")
    if isinstance(meta_dur, (int, float)) and float(meta_dur) > 0:
        record.duration_s = float(meta_dur)
    elif isinstance(meta_dur_ms, (int, float)) and float(meta_dur_ms) > 0:
        record.duration_s = round(float(meta_dur_ms) / 1000.0, 3)
    elif sum_dur > 0:
        record.duration_s = round(sum_dur, 3)
    else:
        record.duration_s = _compute_iso_duration_s(record.started, record.finished)

    if record.engines_label in ("-", ""):
        engines_seen: list[str] = []
        for s in record.steps:
            if not s.is_gate:
                label = s.engine if s.engine not in ("-", "") else s.model
                if label not in ("-", "") and label not in engines_seen:
                    engines_seen.append(label)
        if engines_seen:
            record.engines_label = ",".join(engines_seen)

    for s in reversed(record.steps):
        if s.verdict_line:
            record.verdict_line = s.verdict_line
            break

    if record.outcome in ("-", ""):
        if record.verdict_line:
            record.outcome = record.verdict_line
        elif record.steps:
            record.outcome = record.steps[-1].status
        else:
            record.outcome = "ok"

    if not record.verdict_line and record.outcome not in ("-", ""):
        record.verdict_line = f"OUTCOME: {record.outcome}"


def filter_run_steps(record: RunRecord, step_selector: str) -> list[StepRecord]:
    """Filter ``record.steps`` by ``--step NN`` (matches step number or step ID prefix)."""
    sel = step_selector.strip()
    if not sel:
        return list(record.steps)

    sel_int: int | None = None
    if sel.isdigit():
        sel_int = int(sel)

    matched: list[StepRecord] = []
    for step in record.steps:
        if step.step_id == sel or step.step_num == sel or step.step_id.startswith(sel):
            matched.append(step)
            continue
        if sel_int is not None and step.step_num.isdigit() and int(step.step_num) == sel_int:
            matched.append(step)
    return matched


def format_cost(cost_usd: float) -> str:
    """Format a USD cost float for human display."""
    return f"${cost_usd:.6f}" if 0 < cost_usd < 0.01 else f"${cost_usd:.4f}"


def format_duration(duration_s: float) -> str:
    """Format duration in seconds for human display."""
    return f"{duration_s:.2f}s"


def render_markdown(
    record: RunRecord,
    full: bool = False,
    steps_override: Sequence[StepRecord] | None = None,
) -> str:
    """Render ``record`` (or ``steps_override``) as a self-contained Markdown document."""
    steps = list(steps_override) if steps_override is not None else record.steps
    lines: list[str] = []

    lines.append(f"# Agent Run Log: `{record.run_id}`")
    lines.append("")
    lines.append(
        f"- **Kind:** `{record.kind}` | **Task:** `{record.task_label}` | "
        f"**Engines:** `{record.engines_label}` | **Outcome:** `{record.outcome}`"
    )
    lines.append(
        f"- **Started:** `{record.started}` | **Finished:** `{record.finished}` | "
        f"**Duration:** `{format_duration(record.duration_s)}` | "
        f"**Total Cost:** `{format_cost(record.cost_usd)}`"
    )
    lines.append(
        f"- **Turns:** `{record.total_turns}` | "
        f"**Tokens:** `{record.total_tokens_in} in / {record.total_tokens_out} out`"
    )
    if record.total_protocol_errors > 0:
        lines.append(f"- **Tool Protocol Errors:** `{record.total_protocol_errors}`")
    if record.total_unknown_events > 0:
        lines.append(f"- **Skipped/Unknown Events:** `{record.total_unknown_events}`")
    lines.append("")

    for step in steps:
        if step.is_gate:
            lines.append(f"## Step {step.step_num}: Gate (`{step.step_id}`) — status: `{step.status}`")
            lines.append("")
            lines.append("### Gate Output")
            lines.append("```text")
            lines.append(truncate_text(step.gate_text, full=full))
            lines.append("```")
            if step.verdict_line:
                lines.append("")
                lines.append(f"> **FINAL VERDICT: {step.verdict_line}**")
            lines.append("")
            continue

        lines.append(
            f"## Step {step.step_num}: role=`{step.role}` | engine=`{step.engine}` | "
            f"model=`{step.model}` | status=`{step.status}` | "
            f"duration=`{format_duration(step.duration_s)}` | turns=`{step.num_turns}` | "
            f"tokens=`{step.tokens_in} in / {step.tokens_out} out` | "
            f"cost=`{format_cost(step.cost_usd)}`"
        )
        lines.append("")

        extra_badges: list[str] = []
        if step.cache_read_tokens or step.cache_creation_tokens:
            extra_badges.append(
                f"cache_read={step.cache_read_tokens}, cache_create={step.cache_creation_tokens}"
            )
        if step.prompt_sha256:
            extra_badges.append(f"prompt_sha256={step.prompt_sha256}")
        if step.tool_protocol_errors > 0:
            extra_badges.append(f"tool_protocol_errors={step.tool_protocol_errors}")
        if step.unknown_events > 0:
            extra_badges.append(f"unknown_events={step.unknown_events}")
        if extra_badges:
            lines.append(f"_Metadata: {'; '.join(extra_badges)}_")
            lines.append("")

        if step.prompt_text:
            lines.append("<details>")
            lines.append("<summary>Prompt</summary>")
            lines.append("")
            lines.append("```markdown")
            lines.append(step.prompt_text)
            lines.append("```")
            lines.append("</details>")
            lines.append("")

        for item in step.items:
            if item.kind == "init":
                lines.append(f"- **Session Init:** `{item.text}`")
                lines.append("")
            elif item.kind == "thinking":
                lines.append("<details>")
                lines.append("<summary>Thinking</summary>")
                lines.append("")
                lines.append(truncate_text(item.text, full=full))
                lines.append("")
                lines.append("</details>")
                lines.append("")
            elif item.kind == "user_text":
                lines.append("### User")
                lines.append("")
                lines.append(item.text)
                lines.append("")
            elif item.kind == "text":
                lines.append("### Assistant")
                if item.is_protocol_error:
                    lines.append(
                        "> **[TOOL PROTOCOL ERROR]** Assistant emitted a JSON tool call as plain text without a `tool_use` block."
                    )
                lines.append("")
                lines.append(item.text)
                lines.append("")
            elif item.kind == "tool_use":
                id_suffix = f" (`{item.tool_use_id}`)" if item.tool_use_id else ""
                lines.append(f"### Tool Call: `{item.tool_name}`{id_suffix}")
                lines.append("")
                lines.append("```json")
                lines.append(truncate_text(item.tool_input_json, full=full))
                lines.append("```")
                lines.append("")
            elif item.kind == "tool_result":
                t_label = f"`{item.tool_name}` " if item.tool_name else ""
                err_label = " [ERROR]" if item.is_error else ""
                id_suffix = f"(`{item.tool_use_id}`)" if item.tool_use_id else ""
                lines.append(f"### Tool Result: {t_label}{id_suffix}{err_label}".strip())
                lines.append("")
                lines.append("```text")
                lines.append(truncate_text(item.text, full=full))
                lines.append("```")
                lines.append("")

        if step.gate_text:
            lines.append("### Gate Output")
            lines.append("```text")
            lines.append(truncate_text(step.gate_text, full=full))
            lines.append("```")
            lines.append("")

        if step.final_result:
            lines.append("### Final Result")
            lines.append("")
            lines.append(step.final_result)
            lines.append("")

        if step.verdict_line:
            lines.append(f"> **FINAL VERDICT: {step.verdict_line}**")
            lines.append("")

    if record.verdict_line:
        lines.append("---")
        lines.append(f"> **RUN FINAL VERDICT: {record.verdict_line}**")
        lines.append("")

    return "\n".join(lines)


_HTML_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f8fafc;
  --surface: #ffffff;
  --border: #cbd5e1;
  --text: #0f172a;
  --muted: #475569;
  --accent: #0284c7;
  --code-bg: #f1f5f9;
  --verdict-ok-bg: #dcfce7;
  --verdict-ok-border: #16a34a;
  --verdict-ok-text: #14532d;
  --verdict-warn-bg: #fef2f2;
  --verdict-warn-border: #dc2626;
  --verdict-warn-text: #7f1d1d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f172a;
    --surface: #1e293b;
    --border: #334155;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --code-bg: #0b1120;
    --verdict-ok-bg: #052e16;
    --verdict-ok-border: #22c55e;
    --verdict-ok-text: #dcfce7;
    --verdict-warn-bg: #450a0a;
    --verdict-warn-border: #ef4444;
    --verdict-warn-text: #fecaca;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem;
  font-family: system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}
main { max-width: 1100px; margin: 0 auto; }
h1, h2, h3 { margin-top: 0; }
.summary-card, .step-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem 1.25rem;
  margin-bottom: 1.25rem;
}
.step-header {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1rem;
  font-size: 0.92rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.6rem;
  margin-bottom: 0.85rem;
}
.badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 4px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  font-family: ui-monospace, monospace;
  font-size: 0.85rem;
}
details {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  margin-bottom: 0.75rem;
}
summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--accent);
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.75rem;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, monospace;
  font-size: 0.86rem;
}
.block {
  margin-bottom: 0.85rem;
}
.block-title {
  font-weight: 600;
  margin-bottom: 0.25rem;
  color: var(--muted);
}
.verdict {
  padding: 0.75rem 1rem;
  border-radius: 6px;
  font-weight: 700;
  font-family: ui-monospace, monospace;
  margin-top: 0.75rem;
  border: 2px solid var(--verdict-ok-border);
  background: var(--verdict-ok-bg);
  color: var(--verdict-ok-text);
}
.verdict-fail {
  border-color: var(--verdict-warn-border);
  background: var(--verdict-warn-bg);
  color: var(--verdict-warn-text);
}
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--surface);
  border: 1px solid var(--border);
}
th, td {
  text-align: left;
  padding: 0.6rem 0.8rem;
  border-bottom: 1px solid var(--border);
}
th { background: var(--code-bg); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
""".strip()


def _esc(val: Any) -> str:
    """HTML-escape any value with quote escaping enabled."""
    return html.escape(str(val), quote=True)


def _verdict_css_class(verdict_text: str) -> str:
    """Return CSS class name for a verdict banner based on pass/fail keywords."""
    upper = verdict_text.upper()
    if any(k in upper for k in ("REQUEST_CHANGES", "ESCALATE", "FAIL", "ERROR", "TIMEOUT")):
        return "verdict verdict-fail"
    return "verdict"


def render_html(record: RunRecord, full: bool = False) -> str:
    """Render ``record`` as a single self-contained HTML page with inline CSS and no JS."""
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Agent Run Log - {_esc(record.run_id)}</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        '<section class="summary-card">',
        f"<h1>Agent Run Log: <code>{_esc(record.run_id)}</code></h1>",
        '<div class="step-header">',
        f'<span class="badge">kind: {_esc(record.kind)}</span>',
        f'<span class="badge">task: {_esc(record.task_label)}</span>',
        f'<span class="badge">engines: {_esc(record.engines_label)}</span>',
        f'<span class="badge">outcome: {_esc(record.outcome)}</span>',
        f'<span class="badge">duration: {_esc(format_duration(record.duration_s))}</span>',
        f'<span class="badge">turns: {_esc(record.total_turns)}</span>',
        f'<span class="badge">tokens: {_esc(record.total_tokens_in)} in / {_esc(record.total_tokens_out)} out</span>',
        f'<span class="badge">cost: {_esc(format_cost(record.cost_usd))}</span>',
    ]
    if record.total_protocol_errors > 0:
        parts.append(
            f'<span class="badge">tool_protocol_errors: {_esc(record.total_protocol_errors)}</span>'
        )
    if record.total_unknown_events > 0:
        parts.append(
            f'<span class="badge">unknown_events: {_esc(record.total_unknown_events)}</span>'
        )
    parts.append("</div>")
    if record.verdict_line:
        v_cls = _verdict_css_class(record.verdict_line)
        parts.append(
            f'<div class="{v_cls}">FINAL VERDICT: {_esc(record.verdict_line)}</div>'
        )
    parts.append("</section>")

    for step in record.steps:
        parts.append(f'<section class="step-card" id="step-{_esc(step.step_num)}">')
        if step.is_gate:
            parts.append(
                f"<h2>Step {_esc(step.step_num)}: Gate (<code>{_esc(step.step_id)}</code>)</h2>"
            )
            parts.append('<div class="step-header">')
            parts.append('<span class="badge">role: gate</span>')
            parts.append(f'<span class="badge">status: {_esc(step.status)}</span>')
            parts.append("</div>")
            parts.append('<div class="block">')
            parts.append('<div class="block-title">Gate Output</div>')
            parts.append(f"<pre>{_esc(truncate_text(step.gate_text, full=full))}</pre>")
            parts.append("</div>")
            if step.verdict_line:
                v_cls = _verdict_css_class(step.verdict_line)
                parts.append(
                    f'<div class="{v_cls}">FINAL VERDICT: {_esc(step.verdict_line)}</div>'
                )
            parts.append("</section>")
            continue

        parts.append(
            f"<h2>Step {_esc(step.step_num)}: <code>{_esc(step.role)}</code> "
            f"({_esc(step.engine)})</h2>"
        )
        parts.append('<div class="step-header">')
        parts.append(f'<span class="badge">role: {_esc(step.role)}</span>')
        parts.append(f'<span class="badge">engine: {_esc(step.engine)}</span>')
        parts.append(f'<span class="badge">model: {_esc(step.model)}</span>')
        parts.append(f'<span class="badge">status: {_esc(step.status)}</span>')
        parts.append(
            f'<span class="badge">duration: {_esc(format_duration(step.duration_s))}</span>'
        )
        parts.append(f'<span class="badge">turns: {_esc(step.num_turns)}</span>')
        parts.append(
            f'<span class="badge">tokens: {_esc(step.tokens_in)} in / {_esc(step.tokens_out)} out</span>'
        )
        parts.append(f'<span class="badge">cost: {_esc(format_cost(step.cost_usd))}</span>')
        if step.tool_protocol_errors > 0:
            parts.append(
                f'<span class="badge">tool_protocol_errors: {_esc(step.tool_protocol_errors)}</span>'
            )
        if step.unknown_events > 0:
            parts.append(
                f'<span class="badge">unknown_events: {_esc(step.unknown_events)}</span>'
            )
        parts.append("</div>")

        if step.prompt_text:
            parts.append('<details class="prompt-details">')
            parts.append("<summary>Prompt</summary>")
            parts.append(f"<pre>{_esc(step.prompt_text)}</pre>")
            parts.append("</details>")

        for item in step.items:
            if item.kind == "init":
                parts.append(
                    f'<div class="block"><span class="badge">init: {_esc(item.text)}</span></div>'
                )
            elif item.kind == "thinking":
                parts.append('<details class="thinking-details">')
                parts.append("<summary>Thinking</summary>")
                parts.append(f"<pre>{_esc(truncate_text(item.text, full=full))}</pre>")
                parts.append("</details>")
            elif item.kind == "user_text":
                parts.append('<div class="block">')
                parts.append('<div class="block-title">User</div>')
                parts.append(f"<pre>{_esc(item.text)}</pre>")
                parts.append("</div>")
            elif item.kind == "text":
                parts.append('<div class="block">')
                parts.append('<div class="block-title">Assistant Text</div>')
                if item.is_protocol_error:
                    parts.append(
                        '<div class="verdict verdict-fail">'
                        "TOOL PROTOCOL ERROR: JSON tool call emitted as plain text without tool_use block"
                        "</div>"
                    )
                parts.append(f"<pre>{_esc(item.text)}</pre>")
                parts.append("</div>")
            elif item.kind == "tool_use":
                id_part = f" ({_esc(item.tool_use_id)})" if item.tool_use_id else ""
                parts.append('<div class="block">')
                parts.append(
                    f'<div class="block-title">Tool Call: <code>{_esc(item.tool_name)}</code>{id_part}</div>'
                )
                parts.append(f"<pre>{_esc(truncate_text(item.tool_input_json, full=full))}</pre>")
                parts.append("</div>")
            elif item.kind == "tool_result":
                t_part = f"<code>{_esc(item.tool_name)}</code> " if item.tool_name else ""
                id_part = f"({_esc(item.tool_use_id)})" if item.tool_use_id else ""
                err_part = " [ERROR]" if item.is_error else ""
                parts.append('<div class="block">')
                parts.append(
                    f'<div class="block-title">Tool Result: {t_part}{id_part}{err_part}</div>'
                )
                parts.append(f"<pre>{_esc(truncate_text(item.text, full=full))}</pre>")
                parts.append("</div>")

        if step.gate_text:
            parts.append('<div class="block">')
            parts.append('<div class="block-title">Gate Output</div>')
            parts.append(f"<pre>{_esc(truncate_text(step.gate_text, full=full))}</pre>")
            parts.append("</div>")

        if step.final_result:
            parts.append('<div class="block">')
            parts.append('<div class="block-title">Final Result</div>')
            parts.append(f"<pre>{_esc(step.final_result)}</pre>")
            parts.append("</div>")

        if step.verdict_line:
            v_cls = _verdict_css_class(step.verdict_line)
            parts.append(
                f'<div class="{v_cls}">FINAL VERDICT: {_esc(step.verdict_line)}</div>'
            )

        parts.append("</section>")

    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def discover_run_dirs(runs_root: pathlib.Path) -> list[pathlib.Path]:
    """Return sorted run directories inside ``runs_root``."""
    if not runs_root.is_dir():
        raise FileNotFoundError(f"Runs root directory not found: {runs_root}")

    found: list[pathlib.Path] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "run.json").is_file() or (entry / "steps").is_dir():
            found.append(entry)
    return found


def collect_runs(runs_root: pathlib.Path, redactor: Redactor) -> list[RunRecord]:
    """Load and sort all run records under ``runs_root``."""
    records: list[RunRecord] = []
    for r_dir in discover_run_dirs(runs_root):
        records.append(load_run_dir(r_dir, redactor))
    records.sort(key=lambda r: (r.run_id, r.run_dir.name))
    return records


def format_runs_table(records: Sequence[RunRecord]) -> str:
    """Format a human-readable table of runs with a TOTAL summary row."""
    headers = ("RUN", "KIND", "TASK", "ENGINES", "OUTCOME", "COST", "DURATION")
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    total_cost = 0.0
    total_dur = 0.0

    for rec in records:
        total_cost += rec.cost_usd
        total_dur += rec.duration_s
        rows.append(
            (
                rec.run_id,
                rec.kind,
                rec.task_label,
                rec.engines_label,
                rec.outcome,
                format_cost(rec.cost_usd),
                format_duration(rec.duration_s),
            )
        )

    total_row = (
        f"TOTAL ({len(records)} runs)",
        "-",
        "-",
        "-",
        "-",
        format_cost(total_cost),
        format_duration(total_dur),
    )

    all_for_width = [headers] + rows + [total_row]
    widths = [max(len(r[i]) for r in all_for_width) for i in range(len(headers))]

    def _fmt_row(cols: Sequence[str]) -> str:
        """Pad a tuple of column strings according to computed column widths."""
        return "  ".join(cols[i].ljust(widths[i]) for i in range(len(headers))).rstrip()

    sep = "  ".join("-" * w for w in widths)
    lines = [_fmt_row(headers), sep]
    for r in rows:
        lines.append(_fmt_row(r))
    lines.append(sep)
    lines.append(_fmt_row(total_row))
    return "\n".join(lines)


def format_runs_json(records: Sequence[RunRecord]) -> str:
    """Serialize ``records`` and aggregate totals as pretty JSON."""
    total_cost = round(sum(r.cost_usd for r in records), 8)
    total_dur = round(sum(r.duration_s for r in records), 3)
    payload = {
        "runs": [
            {
                "run_id": r.run_id,
                "dir": r.run_dir.name,
                "kind": r.kind,
                "task": r.task_label,
                "engines": r.engines_label,
                "outcome": r.outcome,
                "cost_usd": r.cost_usd,
                "duration_s": r.duration_s,
            }
            for r in records
        ],
        "totals": {
            "count": len(records),
            "runs": len(records),
            "cost_usd": total_cost,
            "duration_s": total_dur,
        },
    }
    return json.dumps(payload, indent=2)


def render_site_index_html(records: Sequence[RunRecord]) -> str:
    """Render the static site index HTML linking every run under ``runs_root``."""
    total_cost = sum(r.cost_usd for r in records)
    total_dur = sum(r.duration_s for r in records)

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Agent Runs Index</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>Agent Runs Index</h1>",
        "<table>",
        "<thead>",
        "<tr>",
        "<th>Run</th>",
        "<th>Kind</th>",
        "<th>Task</th>",
        "<th>Engines</th>",
        "<th>Outcome</th>",
        "<th>Cost</th>",
        "<th>Duration</th>",
        "<th>Markdown</th>",
        "</tr>",
        "</thead>",
        "<tbody>",
    ]

    for rec in records:
        dir_name = _esc(rec.run_dir.name)
        parts.extend(
            [
                "<tr>",
                f'<td><a href="{dir_name}/index.html"><code>{_esc(rec.run_id)}</code></a></td>',
                f"<td>{_esc(rec.kind)}</td>",
                f"<td>{_esc(rec.task_label)}</td>",
                f"<td><code>{_esc(rec.engines_label)}</code></td>",
                f"<td><code>{_esc(rec.outcome)}</code></td>",
                f"<td><code>{_esc(format_cost(rec.cost_usd))}</code></td>",
                f"<td><code>{_esc(format_duration(rec.duration_s))}</code></td>",
                f'<td><a href="{dir_name}/transcript.md">transcript.md</a></td>',
                "</tr>",
            ]
        )

    parts.extend(
        [
            "</tbody>",
            "<tfoot>",
            "<tr>",
            f"<th>TOTAL ({len(records)} runs)</th>",
            "<th>-</th>",
            "<th>-</th>",
            "<th>-</th>",
            "<th>-</th>",
            f"<th><code>{_esc(format_cost(total_cost))}</code></th>",
            f"<th><code>{_esc(format_duration(total_dur))}</code></th>",
            "<th>-</th>",
            "</tr>",
            "</tfoot>",
            "</table>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(parts) + "\n"


def write_rendered_outputs(record: RunRecord, out_dir: pathlib.Path, full: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """Write ``transcript.md`` and ``index.html`` for ``record`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "transcript.md"
    html_path = out_dir / "index.html"
    md_path.write_text(render_markdown(record, full=full), encoding="utf-8")
    html_path.write_text(render_html(record, full=full), encoding="utf-8")
    return md_path, html_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for ``devpack_logs.py``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--redact-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Environment variable name whose value must be redacted (repeatable).",
    )
    common.add_argument(
        "--patterns",
        default="",
        metavar="FILE",
        help="Optional path to external secret/PII regex pattern file.",
    )
    common.add_argument(
        "--full",
        action="store_true",
        help="Do not truncate tool inputs or tool results at 4 KB.",
    )

    parser = argparse.ArgumentParser(
        prog="devpack_logs.py",
        parents=[common],
        description=(
            "Render, list, show, and build static HTML indexes for agent run directories "
            "and Claude Code session JSONL transcripts."
        ),
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    p_render = subparsers.add_parser(
        "render",
        parents=[common],
        help="Render a run directory or --session file into transcript.md and index.html.",
    )
    p_render.add_argument(
        "run_dir",
        nargs="?",
        default="",
        help="Path to the run directory (omit when using --session).",
    )
    p_render.add_argument(
        "--session",
        default="",
        metavar="FILE.jsonl",
        help="Path to a standalone Claude Code session or stream-json .jsonl file.",
    )
    p_render.add_argument(
        "--out",
        default="",
        metavar="DIR",
        help="Output directory for transcript.md and index.html (required with --session).",
    )

    p_list = subparsers.add_parser(
        "list",
        parents=[common],
        help="List all runs under <runs-root> with outcome, cost, duration, and totals.",
    )
    p_list.add_argument(
        "runs_root",
        help="Root directory containing run subdirectories.",
    )
    p_list.add_argument(
        "--json",
        action="store_true",
        help="Output run list and totals as JSON.",
    )

    p_show = subparsers.add_parser(
        "show",
        parents=[common],
        help="Print a terminal-friendly transcript of <run-dir> (or a single --step NN).",
    )
    p_show.add_argument(
        "run_dir",
        help="Path to the run directory.",
    )
    p_show.add_argument(
        "--step",
        default="",
        metavar="NN",
        help="Optional step number or prefix to display (e.g. 01 or 1).",
    )

    p_site = subparsers.add_parser(
        "site",
        parents=[common],
        help="Render runs and write <runs-root>/index.html linking every run.",
    )
    p_site.add_argument(
        "runs_root",
        help="Root directory containing run subdirectories.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``devpack_logs.py`` CLI."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 2

    redactor = Redactor(
        redact_env_names=args.redact_env,
        patterns_file=args.patterns or None,
    )

    try:
        if args.subcommand == "render":
            if args.session:
                if args.run_dir:
                    print(
                        "ERROR: cannot specify both <run-dir> and --session",
                        file=sys.stderr,
                    )
                    return 2
                if not args.out:
                    print(
                        "ERROR: --out <dir> is required when using --session",
                        file=sys.stderr,
                    )
                    return 2
                session_path = pathlib.Path(args.session)
                out_dir = pathlib.Path(args.out)
                record = load_session_jsonl(session_path, out_dir, redactor)
                write_rendered_outputs(record, out_dir, full=bool(args.full))
                return 0

            if not args.run_dir:
                print(
                    "ERROR: render requires either <run-dir> or --session <file.jsonl> --out <dir>",
                    file=sys.stderr,
                )
                return 2
            run_dir = pathlib.Path(args.run_dir)
            out_dir = pathlib.Path(args.out) if args.out else run_dir
            record = load_run_dir(run_dir, redactor)
            write_rendered_outputs(record, out_dir, full=bool(args.full))
            return 0

        if args.subcommand == "list":
            runs_root = pathlib.Path(args.runs_root)
            records = collect_runs(runs_root, redactor)
            if args.json:
                print(format_runs_json(records))
            else:
                print(format_runs_table(records))
            return 0

        if args.subcommand == "show":
            run_dir = pathlib.Path(args.run_dir)
            record = load_run_dir(run_dir, redactor)
            steps = record.steps
            if args.step:
                steps = filter_run_steps(record, args.step)
                if not steps:
                    print(
                        f"ERROR: no step matching '{args.step}' in {run_dir}",
                        file=sys.stderr,
                    )
                    return 1
            print(render_markdown(record, full=bool(args.full), steps_override=steps))
            return 0

        if args.subcommand == "site":
            runs_root = pathlib.Path(args.runs_root)
            records = collect_runs(runs_root, redactor)
            for rec in records:
                write_rendered_outputs(rec, rec.run_dir, full=bool(args.full))
            site_html = render_site_index_html(records)
            (runs_root / "index.html").write_text(site_html, encoding="utf-8")
            return 0

    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
