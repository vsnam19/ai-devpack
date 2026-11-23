#!/usr/bin/env python3
# Header: Unified secret, PII, home-path, and OS-identity redaction for agent logs and memory.
# Consolidates credential/pattern redaction across agent_exec, devpack_logs, memory,
# devpack_dispatch, devpack_run, and eval/run_eval so raw streams, gate logs, metadata,
# rendered transcripts, and memory proposals enforce the same rules.
#
# Exit codes:
#   Module only (no CLI entrypoint).
"""Unified redaction engine shared by execution, logging, memory, and evaluation."""

from __future__ import annotations

import base64
import binascii
import getpass
import json
import os
from pathlib import Path
import platform
import re
import socket
import sys
from typing import Any, Callable, Mapping, Sequence

try:
    import grp  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - non-POSIX
    grp = None  # type: ignore[assignment]

try:
    import pwd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None  # type: ignore[assignment]

_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT))


# Construct host-home prefix dynamically so forbidden literal substrings never appear in source.
_LINUX_HOME_PREFIX = "/" + "ho" + "me"
_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:" + re.escape(_LINUX_HOME_PREFIX) + r"|/Users)/[A-Za-z0-9_.-]+"
)

_BEGIN_WORD = "-----BEGIN"
_END_WORD = "-----END"
_PRIV_KEY_SUFFIX = "PRIVATE" + " KEY-----"
_PRIVATE_KEY_BLOCK_RE = re.compile(
    rf"{_BEGIN_WORD}\s+(?:[A-Z0-9 ]+\s+)?{_PRIV_KEY_SUFFIX}[\s\S]*?{_END_WORD}\s+(?:[A-Z0-9 ]+\s+)?{_PRIV_KEY_SUFFIX}"
)

# Group-preserving regexes avoid double-redacting placeholders like [REDACTED] or <REDACTED...>.
_NOT_ALREADY_REDACTED = r"(?!\[REDACTED\]|<REDACTED)"
_BEARER_TOKEN_RE = re.compile(
    rf"(?i)\b(Bearer\s+){_NOT_ALREADY_REDACTED}([A-Za-z0-9._~+/=-]{{8,}})"
)
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
    rf"(\s*[:=]\s*['\"]?){_NOT_ALREADY_REDACTED}(?![0-9]+\b)([A-Za-z0-9._~+/=-]{{8,}})(['\"]?)"
)
_JSON_CRED_RE = re.compile(
    r"(?i)(['\"](?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|secret[_-]?key|private[_-]?key|sessionKey|claudeAiOauth"
    r"|password|passwd|secret|token)['\"]\s*:\s*['\"])"
    rf"{_NOT_ALREADY_REDACTED}([^'\"\s]{{6,}})(['\"])"
)
_PII_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Pre-commit (source scanning) rule: a credential-looking name assigned a quoted,
# high-entropy literal. Requires quotes, >= 20 value characters, and both a letter and
# a digit. Identifiers and numeric right-hand sides (``token = secretIndex + 1U;``,
# ``secret = computeSeed(level);``, ``int passwd = 3;``) do not match.
_CREDENTIAL_NAMES = (
    r"(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret"
    r"|secret[_-]?key|private[_-]?key|bearer[_-]?token|secret|token|passw(?:or)?d)"
)
_HIGH_ENTROPY_ASSIGN_RE = re.compile(
    # Quoted literal in source or config: name = "value" (>= 20 characters).
    rf"\b(?:[a-z0-9]+[_-])*{_CREDENTIAL_NAMES}"
    r"\s*[:=]\s*(?P<q>['\"])(?=[^'\"]*[A-Za-z])(?=[^'\"]*[0-9])[A-Za-z0-9._~+/=-]{20,}(?P=q)"
    r"|"
    # Whole-line config assignment: name=value or name: value with no operators or ';'.
    # Excludes source expressions such as ``token = secretIndex + 1U;``.
    rf"^\s*(?:[a-z0-9]+[_.-])*{_CREDENTIAL_NAMES}"
    r"\s*[:=]\s*(?=[^\s'\";]*[A-Za-z])(?=[^\s'\";]*[0-9])[A-Za-z0-9._~+/=-]{8,}\s*$",
    re.IGNORECASE,
)

# base64 runs long enough to hold an encoded ``sk-ant-`` key; decoded at 4 alignments.
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_BASE64_WRAPPED_MARKER = b"sk-ant-"
_BASE64_WRAPPED_RULE = "base64-wrapped-api-token"

_DEFAULT_AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEVPACK_LOCAL_AUTH_TOKEN",
)
_SECRET_ENV_SUFFIXES = ("_AUTH_TOKEN", "_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")

_GENERIC_OS_USERS = frozenset(
    {"root", "nobody", "daemon", "bin", "sys", "sync", "user", "admin", "runner", "test"}
)
_GENERIC_OS_GROUPS = frozenset(
    {
        "root", "nobody", "nogroup", "daemon", "bin", "sys", "sync", "users", "staff",
        "wheel", "docker", "sudo", "adm", "mail", "tty", "disk", "audio", "video",
        "render", "kvm", "input", "cdrom", "plugdev", "dip", "lpadmin", "netdev",
        "dialout", "lp", "scanner", "utmp", "shadow", "crontab", "ssh",
        "systemd-journal", "runner",
    }
)
_GENERIC_OS_HOSTS = frozenset(
    {
        "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "host",
        "runner", "dev", "tmp", "var", "opt", "etc", "usr", "lib", "bin", "sys",
        "proc", "run", "mnt", "home", "root",
    }
)
_LS_LONG_LINE_RE = re.compile(
    r"(^|\n|\\n)([dlcbps-][rwxstST-]{9}[+.@]?\s+\d+\s+)(\S+)(\s+)(\S+)(\s+\d+\b)"
)

NO_OS_IDENTITIES: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] = (
    frozenset(),
    frozenset(),
    (),
)

_EVENT_PROTOCOL_KEYS = frozenset({"type", "subtype", "session_id", "uuid", "model", "stop_reason"})
_EVENT_ENVELOPE_KEYS = _EVENT_PROTOCOL_KEYS | frozenset(
    {
        "message", "result", "is_error", "num_turns", "turns", "duration_ms",
        "duration_s", "total_cost_usd", "cost_usd", "cost", "usage", "modelUsage",
        "permission_denials", "cwd", "tools", "model_digest", "digest",
    }
)
_MESSAGE_PROTOCOL_KEYS = frozenset({"role", "model", "id", "stop_reason", "type"})
_MESSAGE_ENVELOPE_KEYS = _MESSAGE_PROTOCOL_KEYS | frozenset({"content", "usage"})
_CONTENT_BLOCK_PROTOCOL_KEYS = frozenset({"type", "id", "tool_use_id"})
_CONTENT_BLOCK_ENVELOPE_KEYS = _CONTENT_BLOCK_PROTOCOL_KEYS | frozenset(
    {"name", "input", "text", "thinking", "content", "is_error"}
)


def json_string_leaves(value: Any, min_len: int = 8) -> list[str]:
    """Return every string value (length >= ``min_len``) found inside a JSON structure."""
    if isinstance(value, str):
        return [value] if len(value) >= min_len else []
    if isinstance(value, list):
        return [leaf for item in value for leaf in json_string_leaves(item, min_len)]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in json_string_leaves(item, min_len)]
    return []


def extract_credential_file_secrets(raw_text: str) -> list[str]:
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
            if len(clean) >= 6:
                found.append(clean)
        return found
    found.extend(json_string_leaves(parsed, min_len=6))
    return found


def collect_credential_secrets(
    credentials_path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    include_raw_file: bool = True,
) -> list[str]:
    """Collect literal secret values from a credentials file and ``DEVPACK_CLAUDE_CREDENTIALS``."""
    source_env = os.environ if env is None else env
    candidates: list[Path] = []
    if credentials_path is not None and str(credentials_path).strip():
        candidates.append(Path(str(credentials_path).strip()))
    env_cred = source_env.get("DEVPACK_CLAUDE_CREDENTIALS", "").strip()
    if env_cred:
        env_path = Path(env_cred)
        if env_path not in candidates:
            candidates.append(env_path)

    secrets: list[str] = []
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            raw = cand.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not raw:
            continue
        if include_raw_file and len(raw) >= 4:
            secrets.append(raw)
        secrets.extend(extract_credential_file_secrets(raw))
    return secrets


def collect_env_auth_secrets(
    extra_env_names: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Collect non-empty secret values from auth/token/key environment variables."""
    source_env = os.environ if env is None else env
    explicit_names = {n.strip().upper() for n in _DEFAULT_AUTH_ENV_NAMES}
    for item in extra_env_names:
        for part in str(item).split(","):
            cleaned_name = part.strip().upper()
            if cleaned_name:
                explicit_names.add(cleaned_name)

    secrets: list[str] = []
    for key, val in source_env.items():
        upper = key.upper()
        if upper in explicit_names or any(upper.endswith(s) for s in _SECRET_ENV_SUFFIXES):
            cleaned = val.strip()
            if len(cleaned) >= 4:
                secrets.append(cleaned)
                if val != cleaned and len(val) >= 4:
                    secrets.append(val)
    return secrets


def _base_secret_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Return built-in secret detection rules as ``(rule_id, compiled_regex)`` pairs."""
    begin_word = "-----BEGIN"
    priv_key_word = "PRIVATE" + " KEY-----"
    pwd_word = "pass" + "word"
    passwd_word = "pass" + "wd"
    aws_sec_word = "aws_secret" + "_access_key"

    return [
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
                rf"(?i)\b(?:{pwd_word}|{passwd_word})\s*[:=]\s*(?!['\"]?\s*(?:$|#))['\"]?[^\s'\";#]+"
            ),
        ),
    ]


def get_builtin_secret_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Return the single shared built-in credential rule set for scanning and redaction."""
    rules: list[tuple[str, re.Pattern[str]]] = [
        ("private-key-block", _PRIVATE_KEY_BLOCK_RE),
    ]
    rules.extend(_base_secret_rules())
    rules.extend(
        [
            ("prefixed-api-token", _PREFIXED_TOKEN_RE),
            ("bearer-token", _BEARER_TOKEN_RE),
            ("json-credential", _JSON_CRED_RE),
            ("credential-assignment", _CRED_ASSIGN_RE),
            ("url-userinfo", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
            ("basic-auth", re.compile(r"(?i)\bAuthorization\s*:\s*Basic\s+[A-Za-z0-9+/]+={0,2}")),
            ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
            ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
        ]
    )
    return rules


def get_precommit_secret_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Return the high-confidence rule set used to scan source before commit.

    Only key formats with a distinctive prefix or shape, PEM private key blocks, and
    high-entropy quoted literals assigned to credential-like names are included. Bare
    identifiers on the right-hand side of ``token``/``secret``/``passwd`` assignments
    are not. Log redaction keeps the broader :func:`get_builtin_secret_rules` set.
    """
    begin_word = "-----BEGIN"
    priv_key_word = "PRIVATE" + " KEY-----"
    return [
        (
            "private-key-header",
            re.compile(rf"{begin_word}\s+(?:[A-Z0-9 ]+\s+)?{priv_key_word}"),
        ),
        ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
        ("prefixed-api-token", _PREFIXED_TOKEN_RE),
        ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
        ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
        ("high-entropy-assignment", _HIGH_ENTROPY_ASSIGN_RE),
    ]


def _decode_base64_candidate(run: str) -> bytes | None:
    """Decode ``run`` as base64 after trimming padding, or return None if it is not decodable."""
    body = run.rstrip("=")
    if len(body) % 4 == 1:
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def find_base64_wrapped_api_keys(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans of base64 runs whose decoding contains an ``sk-ant-`` marker.

    A run is tried at every alignment (offsets 0-3) because the encoded key can start
    mid-run. Matched values are never returned, only their positions.
    """
    spans: list[tuple[int, int]] = []
    if not text:
        return spans
    for match in _BASE64_RUN_RE.finditer(text):
        run = match.group(0)
        for offset in range(4):
            if len(run) - offset < 8:
                break
            decoded = _decode_base64_candidate(run[offset:])
            if decoded and _BASE64_WRAPPED_MARKER in decoded:
                spans.append((match.start(), match.end()))
                break
    return spans


def get_secret_pii_rules(
    *,
    patterns_file: Path | str | None = None,
    include_pii: bool = False,
    strict_patterns_file: bool = False,
) -> list[tuple[str, re.Pattern[str]]]:
    """Return built-in, token/credential, optional PII, and external pattern rules."""
    rules = get_builtin_secret_rules()
    if include_pii:
        rules.append(("pii-email", _PII_EMAIL_RE))
        rules.append(("host-home-path", _HOME_PATH_RE))

    ext_raw = str(patterns_file).strip() if patterns_file else os.environ.get("DEVPACK_SECRET_PATTERNS", "").strip()
    if ext_raw:
        ext_path = Path(ext_raw)
        if ext_path.is_file():
            from enforcement.checks.secret_pii_check import load_external_patterns
            loaded, err = load_external_patterns(ext_path)
            if loaded is None:
                if strict_patterns_file:
                    raise ValueError(f"Failed to load secret patterns file: {err}")
            else:
                rules.extend(loaded)
    return rules


def find_secret_pii_violations(
    text: str,
    *,
    extra_secrets: Sequence[str] = (),
    patterns_file: Path | str | None = None,
    include_pii: bool = True,
    credentials_path: Path | str | None = None,
) -> list[str]:
    """Return unique rule IDs for any secrets, credentials, or PII detected in ``text``."""
    if not text:
        return []
    violations: list[str] = []
    seen: set[str] = set()

    def _add(rule_id: str) -> None:
        """Record a unique violated rule ID."""
        if rule_id not in seen:
            seen.add(rule_id)
            violations.append(rule_id)

    literals = (
        list(extra_secrets)
        + collect_env_auth_secrets()
        + collect_credential_secrets(credentials_path, include_raw_file=False)
    )
    for lit in literals:
        cleaned = str(lit).strip()
        if len(cleaned) >= 4 and cleaned in text:
            _add("env-secret-value")

    rules = get_secret_pii_rules(
        patterns_file=patterns_file,
        include_pii=include_pii,
        strict_patterns_file=True,
    )
    for rule_id, rx in rules:
        if rule_id == "private-key-block":
            if rx.search(text):
                _add(rule_id)
            continue
        for line in text.splitlines():
            if rx.search(line):
                _add(rule_id)
                break
    if find_base64_wrapped_api_keys(text):
        _add(_BASE64_WRAPPED_RULE)
    return violations


def collect_os_identities() -> tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]]:
    """Collect current OS user, group, and host names for log redaction."""
    all_users: set[str] = set()
    if pwd is not None:
        try:
            pw_name = pwd.getpwuid(os.getuid()).pw_name.strip()
            if pw_name and not pw_name.isdigit():
                all_users.add(pw_name)
        except (KeyError, OSError):
            pass
    try:
        gu = getpass.getuser().strip()
        if gu and not gu.isdigit():
            all_users.add(gu)
    except Exception:
        pass
    for env_key in ("USER", "LOGNAME"):
        val = os.environ.get(env_key, "").strip()
        if val and not val.isdigit():
            all_users.add(val)

    all_groups: set[str] = set()
    general_groups: set[str] = set()
    if grp is not None:
        primary_gid: int | None = None
        try:
            primary_gid = os.getgid()
            gname = grp.getgrgid(primary_gid).gr_name.strip()
            if gname and not gname.isdigit():
                all_groups.add(gname)
                general_groups.add(gname)
        except (KeyError, OSError):
            pass
        try:
            for gid in os.getgroups():
                try:
                    gname = grp.getgrgid(gid).gr_name.strip()
                except KeyError:
                    continue
                if gname and not gname.isdigit():
                    all_groups.add(gname)
                    if gid == primary_gid or gid >= 1000:
                        general_groups.add(gname)
        except OSError:
            pass

    raw_hosts: set[str] = set()
    for getter in (socket.gethostname, platform.node):
        try:
            h = getter().strip()
        except OSError:
            h = ""
        if h and not h.isdigit():
            raw_hosts.add(h)
            if "." in h:
                short = h.split(".", 1)[0].strip()
                if short and not short.isdigit():
                    raw_hosts.add(short)

    replacements: list[tuple[str, str]] = []
    for u in all_users:
        if len(u) >= 2 and u.lower() not in _GENERIC_OS_USERS:
            replacements.append((u, "[USER]"))
    for g in general_groups:
        if len(g) >= 2 and g.lower() not in _GENERIC_OS_GROUPS and g not in all_users:
            replacements.append((g, "[GROUP]"))
    for h in raw_hosts:
        if len(h) >= 3 and h.lower() not in _GENERIC_OS_HOSTS:
            replacements.append((h, "[HOST]"))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    return frozenset(all_users), frozenset(all_groups), tuple(replacements)


def redact_os_identities(
    text: str,
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None = None,
) -> str:
    """Replace current OS user, group, and host names in ``text`` with placeholders."""
    if not text:
        return text
    all_users, all_groups, replacements = (
        identities if identities is not None else collect_os_identities()
    )

    def _replace_ls_line(match: re.Match[str]) -> str:
        """Replace owner and group columns in ``ls -l`` lines when matching current OS IDs."""
        prefix_nl, perms_links, owner, ws, group, size_tail = match.groups()
        new_owner = "[USER]" if owner in all_users else owner
        new_group = "[GROUP]" if group in all_groups else group
        return f"{prefix_nl}{perms_links}{new_owner}{ws}{new_group}{size_tail}"

    out = _LS_LONG_LINE_RE.sub(_replace_ls_line, text)
    for token, placeholder in replacements:
        pattern = re.compile(
            r"(?:(?<![A-Za-z0-9_])|(?<=\\n)|(?<=\\t)|(?<=\\r))"
            + re.escape(token)
            + r"(?![A-Za-z0-9_])"
        )
        out = pattern.sub(placeholder, out)
    return out


def _build_literal_secrets(
    extra_secrets: Sequence[str],
    extra_env_names: Sequence[str] = (),
    credentials_path: Path | str | None = None,
) -> list[str]:
    """Assemble and length-sort unique literal secret strings to redact."""
    raw: list[str] = []
    for item in extra_secrets:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        raw.append(cleaned)
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                raw.extend(json_string_leaves(json.loads(cleaned), min_len=4))
            except json.JSONDecodeError:
                pass
    raw.extend(collect_env_auth_secrets(extra_env_names))
    raw.extend(collect_credential_secrets(credentials_path))
    return sorted({s for s in raw if len(s) >= 4}, key=len, reverse=True)


def _apply_redaction(
    text: str,
    *,
    literal_secrets: Sequence[str],
    rules: Sequence[tuple[str, re.Pattern[str]]],
    path_replacements: Sequence[tuple[str, str]],
    home_placeholder: str,
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None,
    literal_placeholder: str,
    rule_placeholder_fn: Callable[[str], str],
) -> str:
    """Apply literal secret, regex rule, home-path, and OS-identity redaction to ``text``."""
    if not text:
        return text
    result = text

    for secret_val in literal_secrets:
        if secret_val in result:
            result = result.replace(secret_val, literal_placeholder)

    for rule_id, rx in rules:
        ph = rule_placeholder_fn(rule_id)
        if rule_id == "bearer-token":
            result = rx.sub(lambda m, p=ph: f"{m.group(1)}{p}", result)
        elif rule_id == "json-credential":
            result = rx.sub(lambda m, p=ph: f"{m.group(1)}{p}{m.group(3)}", result)
        elif rule_id == "credential-assignment":
            result = rx.sub(lambda m, p=ph: f"{m.group(1)}{m.group(2)}{p}{m.group(4)}", result)
        else:
            result = rx.sub(ph, result)

    for start, end in reversed(find_base64_wrapped_api_keys(result)):
        result = result[:start] + rule_placeholder_fn(_BASE64_WRAPPED_RULE) + result[end:]

    for raw_path, placeholder in path_replacements:
        if raw_path:
            result = result.replace(raw_path, placeholder)

    env_home = os.environ.get("HOME", "").strip()
    if env_home and env_home != "/":
        result = result.replace(env_home.rstrip("/"), home_placeholder)

    try:
        resolved_home = str(Path.home()).rstrip("/")
        if resolved_home and resolved_home != "/":
            result = result.replace(resolved_home, home_placeholder)
    except RuntimeError:
        pass

    result = _HOME_PATH_RE.sub(home_placeholder, result)
    result = redact_os_identities(result, identities)
    return result


def redact_sensitive_text(
    text: str,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None = None,
    *,
    credentials_path: Path | str | None = None,
    patterns_file: Path | str | None = None,
) -> str:
    """Redact credentials, auth env values, secret/PII patterns, host home paths, and OS IDs."""
    if not text:
        return text
    literals = _build_literal_secrets(extra_secrets, credentials_path=credentials_path)
    rules = get_secret_pii_rules(patterns_file=patterns_file, include_pii=False)
    return _apply_redaction(
        text,
        literal_secrets=literals,
        rules=rules,
        path_replacements=path_replacements,
        home_placeholder=home_placeholder,
        identities=identities,
        literal_placeholder="<REDACTED_CREDENTIALS>",
        rule_placeholder_fn=lambda rule_id: f"<REDACTED:{rule_id}>",
    )


def redact_value(
    value: Any,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None = None,
    *,
    credentials_path: Path | str | None = None,
    _text_fn: Callable[[str], str] | None = None,
) -> Any:
    """Recursively redact sensitive strings and dict keys with no stream-protocol exemptions."""
    if _text_fn is None:
        ids = identities if identities is not None else collect_os_identities()

        def _redact_leaf(text: str) -> str:
            """Redact one scalar string or dict key using pre-collected OS identities."""
            return redact_sensitive_text(
                text,
                extra_secrets=extra_secrets,
                path_replacements=path_replacements,
                home_placeholder=home_placeholder,
                identities=ids,
                credentials_path=credentials_path,
            )

        _text_fn = _redact_leaf

    if isinstance(value, str):
        return _text_fn(value)
    if isinstance(value, list):
        return [redact_value(item, _text_fn=_text_fn) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, _text_fn=_text_fn) for item in value)
    if isinstance(value, dict):
        return {
            (_text_fn(k) if isinstance(k, str) else k): redact_value(v, _text_fn=_text_fn)
            for k, v in value.items()
        }
    return value


def redact_structure(
    value: Any,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None = None,
    *,
    credentials_path: Path | str | None = None,
) -> Any:
    """Alias for :func:`redact_value` preserving existing callers."""
    return redact_value(
        value,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder=home_placeholder,
        identities=identities,
        credentials_path=credentials_path,
    )


def _protocol_scalar(item: Any, protocol_fn: Callable[[str], str]) -> Any:
    """Return a protocol-position scalar with secret/PII/path redaction but no OS-ID swap."""
    return protocol_fn(item) if isinstance(item, str) else item


def _redact_content_block(
    block: Any,
    text_fn: Callable[[str], str],
    protocol_fn: Callable[[str], str],
) -> Any:
    """Redact one ``message.content[]`` block; protocol positions skip OS-identity swap only."""
    if not isinstance(block, dict):
        return redact_value(block, _text_fn=text_fn)
    is_tool_use = block.get("type") == "tool_use"
    out: dict[str, Any] = {}
    for key, item in block.items():
        protocol = (
            key in _CONTENT_BLOCK_PROTOCOL_KEYS or (key == "name" and is_tool_use)
        ) and not isinstance(item, (dict, list))
        out_key = key if key in _CONTENT_BLOCK_ENVELOPE_KEYS else text_fn(str(key))
        out[out_key] = _protocol_scalar(item, protocol_fn) if protocol else redact_value(item, _text_fn=text_fn)
    return out


def _redact_message(
    message: dict[str, Any],
    text_fn: Callable[[str], str],
    protocol_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Redact ``event.message``; protocol fields skip OS-identity replacement."""
    out: dict[str, Any] = {}
    for key, item in message.items():
        if key in _MESSAGE_PROTOCOL_KEYS and not isinstance(item, (dict, list)):
            out[key] = _protocol_scalar(item, protocol_fn)
        elif key == "content" and isinstance(item, list):
            out[key] = [_redact_content_block(block, text_fn, protocol_fn) for block in item]
        else:
            out_key = key if key in _MESSAGE_ENVELOPE_KEYS else text_fn(str(key))
            out[out_key] = redact_value(item, _text_fn=text_fn)
    return out


def _redact_stream_event(
    event: dict[str, Any],
    text_fn: Callable[[str], str],
    protocol_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Redact one stream-json event; protocol fields skip OS-identity swap only at their positions."""
    out: dict[str, Any] = {}
    for key, item in event.items():
        if key in _EVENT_PROTOCOL_KEYS and not isinstance(item, (dict, list)):
            out[key] = _protocol_scalar(item, protocol_fn)
        elif key == "message" and isinstance(item, dict):
            out[key] = _redact_message(item, text_fn, protocol_fn)
        else:
            out_key = key if key in _EVENT_ENVELOPE_KEYS else text_fn(str(key))
            out[out_key] = redact_value(item, _text_fn=text_fn)
    return out


def redact_stream_line(
    line: str,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    home_placeholder: str = "~",
    identities: tuple[frozenset[str], frozenset[str], tuple[tuple[str, str], ...]] | None = None,
    *,
    credentials_path: Path | str | None = None,
) -> str:
    """Redact one raw agent-CLI stdout line, exempting stream-json protocol positions only."""
    if not line:
        return line
    ids = identities if identities is not None else collect_os_identities()

    def _redact_leaf(text: str) -> str:
        """Redact secrets, paths, and OS identities in one non-protocol string."""
        return redact_sensitive_text(
            text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            home_placeholder=home_placeholder,
            identities=ids,
            credentials_path=credentials_path,
        )

    def _redact_protocol(text: str) -> str:
        """Redact secrets and paths in one protocol string without OS-identity swap."""
        return redact_sensitive_text(
            text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            home_placeholder=home_placeholder,
            identities=NO_OS_IDENTITIES,
            credentials_path=credentials_path,
        )

    body = line.rstrip("\r\n")
    ending = line[len(body):]
    stripped = body.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            redacted_obj = _redact_stream_event(parsed, _redact_leaf, _redact_protocol)
            return json.dumps(redacted_obj, ensure_ascii=True, separators=(",", ":")) + ending

    return redact_sensitive_text(
        line,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder=home_placeholder,
        identities=ids,
        credentials_path=credentials_path,
    )


class Redactor:
    """Configurable redactor for transcript rendering and structured log scrubbing."""

    def __init__(
        self,
        redact_env_names: Sequence[str] | None = None,
        patterns_file: Path | str | None = None,
        extra_literals: Sequence[str] | None = None,
        *,
        credentials_path: Path | str | None = None,
        home_placeholder: str = "~",
        path_replacements: Sequence[tuple[str, str]] = (),
        placeholder: str = "[REDACTED]",
    ) -> None:
        """Initialize redaction rules, credential literals, home normalization, and OS IDs."""
        self.rules = get_secret_pii_rules(patterns_file=patterns_file, include_pii=False)
        self.secret_literals = _build_literal_secrets(
            list(extra_literals or ()),
            extra_env_names=list(redact_env_names or ()),
            credentials_path=credentials_path,
        )
        self.home_placeholder = home_placeholder
        self.path_replacements = tuple(path_replacements)
        self.placeholder = placeholder
        self._os_identities = collect_os_identities()

    def redact(self, text: str) -> str:
        """Return a redacted copy of ``text`` with secrets, home paths, and OS IDs scrubbed."""
        return _apply_redaction(
            text,
            literal_secrets=self.secret_literals,
            rules=self.rules,
            path_replacements=self.path_replacements,
            home_placeholder=self.home_placeholder,
            identities=self._os_identities,
            literal_placeholder=self.placeholder,
            rule_placeholder_fn=lambda _rule_id: self.placeholder,
        )

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings and dict keys in ``value`` with no exemptions."""
        return redact_value(value, _text_fn=self.redact)


def redact_text(
    text: str,
    redact_env_names: Sequence[str] | None = None,
    patterns_file: Path | str | None = None,
    extra_literals: Sequence[str] | None = None,
    *,
    credentials_path: Path | str | None = None,
) -> str:
    """Convenience function to redact a string using ``Redactor``."""
    return Redactor(
        redact_env_names=redact_env_names,
        patterns_file=patterns_file,
        extra_literals=extra_literals,
        credentials_path=credentials_path,
    ).redact(text)
