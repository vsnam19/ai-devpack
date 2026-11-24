#!/usr/bin/env python3
# checkpoints.py - Pipeline operating mode resolution, checkpoint approvals, and provenance recording.
#
# Usage:
#   Library module imported by devpack_run, devpack_dispatch, devpack_broker,
#   devpack_memory, and devpack_approve.
#
# Exit codes:
#   Module only (AWAITING_APPROVAL_EXIT_CODE = 20 is used by callers stopping at a
#   blocking checkpoint).
"""Pipeline operating modes (hitl / autonomous) and recorded checkpoint approvals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence
import uuid

try:
    import pwd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None  # type: ignore[assignment]

RUNNER_DIR = Path(__file__).resolve().parent
PACK_ROOT = RUNNER_DIR.parent
for _p in (str(PACK_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from runner.redaction import redact_sensitive_text, redact_value  # noqa: E402
    from runner.safe_git import GitSecurityError, run_git  # noqa: E402
    from runner.workspace_scope import parse_toml_with_arrays  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover
    from redaction import redact_sensitive_text, redact_value  # type: ignore[no-redef] # noqa: E402
    from safe_git import GitSecurityError, run_git  # type: ignore[no-redef] # noqa: E402
    from workspace_scope import parse_toml_with_arrays  # type: ignore[no-redef] # noqa: E402


AWAITING_APPROVAL_EXIT_CODE = 20
AWAITING_APPROVAL = "AWAITING_APPROVAL"

APPROVED = "APPROVED"
PENDING = "PENDING"
REJECTED = "REJECTED"

DEFAULT_MODE = "hitl"
DEFAULT_TARGET_BRANCH = "main"
DEFAULT_PROTECTED_BRANCHES: tuple[str, ...] = ("main", "master", "release/*", "hotfix/*")
VALID_MODES: tuple[str, ...] = ("hitl", "autonomous")
VALID_CHECKPOINT_OVERRIDES: tuple[str, ...] = ("auto", "human")
MODE_STRICTNESS: dict[str, int] = {"autonomous": 0, "hitl": 1}

CHECKPOINT_SPECS: dict[str, dict[str, Any]] = {
    "plan": {
        "stage": "plan",
        "kind": "blocking",
        "always_hard_stop": False,
        "conditional_hard_stop": False,
        "description": "Task plan before brief drafting",
    },
    "brief": {
        "stage": "brief",
        "kind": "blocking",
        "always_hard_stop": False,
        "conditional_hard_stop": False,
        "description": "Task brief before worker dispatch",
    },
    "deviation": {
        "stage": "deviation",
        "kind": "blocking",
        "always_hard_stop": False,
        "conditional_hard_stop": True,
        "description": "New deviation registration (hard-stop if ASIL >= B or security-relevant)",
    },
    "selinux-manifest": {
        "stage": "selinux-manifest",
        "kind": "blocking",
        "always_hard_stop": False,
        "conditional_hard_stop": True,
        "description": "SELinux manifest change (hard-stop for release builds)",
    },
    "pre-merge": {
        "stage": "pre-merge",
        "kind": "terminal",
        "always_hard_stop": False,
        "conditional_hard_stop": True,
        "description": "Change ready for merge (hard-stop into protected branches; agents never merge)",
    },
    "escalation": {
        "stage": "escalation",
        "kind": "terminal",
        "always_hard_stop": True,
        "conditional_hard_stop": False,
        "description": "Escalation to human after fix rounds exhausted or STOP condition",
    },
    "memory-decision": {
        "stage": "memory",
        "kind": "terminal",
        "always_hard_stop": True,
        "conditional_hard_stop": False,
        "description": "Acceptance of decision/fact memory entries",
    },
}

ALWAYS_HARD_STOP_CHECKPOINTS: frozenset[str] = frozenset(
    cid for cid, spec in CHECKPOINT_SPECS.items() if spec["always_hard_stop"]
)
BLOCKING_CHECKPOINTS: frozenset[str] = frozenset(
    cid for cid, spec in CHECKPOINT_SPECS.items() if spec["kind"] == "blocking"
)
TERMINAL_CHECKPOINTS: frozenset[str] = frozenset(
    cid for cid, spec in CHECKPOINT_SPECS.items() if spec["kind"] == "terminal"
)

_HIGH_ASIL_VALUES: frozenset[str] = frozenset(
    {"B", "C", "D", "ASIL-B", "ASIL-C", "ASIL-D", "ASIL_B", "ASIL_C", "ASIL_D", "UNKNOWN"}
)
_PROTECTED_BRANCH_PREFIXES: tuple[str, ...] = ("release/", "hotfix/")
_PROTECTED_BRANCH_NAMES: frozenset[str] = frozenset({"main", "master"})
_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FAILURE_TEXT_MARKERS: tuple[str, ...] = (
    "GATE_FAIL",
    "SCOPE_VIOLATION",
    "TEST_LOCK_VIOLATION",
    "OUT_OF_SCOPE_CHANGE",
    "GIT_METADATA_TAMPERED",
)


@dataclass(frozen=True)
class PipelineConfig:
    """Resolved ``[pipeline]`` configuration from trusted project settings."""

    mode: str = DEFAULT_MODE
    state_dir: str = ""
    state_dir_explicit: bool = False
    target_branch: str = DEFAULT_TARGET_BRANCH
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    checkpoints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """Outcome of a checkpoint request or human approval decision."""

    approval_id: str
    checkpoint_id: str
    status: str
    kind: str
    hard_stop: bool
    decided_by: str
    record_path: Path
    subject: str = ""
    summary: str = ""
    evidence_paths: tuple[str, ...] = ()
    note: str = ""
    created: str = ""
    decided_at: str = ""
    mode: str = DEFAULT_MODE
    gate_receipts: tuple[dict[str, str], ...] = ()

    @property
    def approved(self) -> bool:
        """Return True when the checkpoint decision is APPROVED."""
        return self.status == APPROVED

    @property
    def pending(self) -> bool:
        """Return True when the checkpoint decision is PENDING."""
        return self.status == PENDING

    @property
    def rejected(self) -> bool:
        """Return True when the checkpoint decision is REJECTED."""
        return self.status == REJECTED


def _now_utc_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, redact: bool = True) -> None:
    """Atomically write JSON (redacted by default) to ``path`` without following symlinks."""
    if path.is_symlink():
        raise ValueError(f"Refusing to write checkpoint record over symlink: {path}")
    for parent in (path.parent, path.parent.parent):
        if parent.is_symlink():
            raise ValueError(f"Refusing to write inside symlinked directory: {parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = redact_value(dict(payload)) if redact else dict(payload)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(safe_payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _append_jsonl_line(path: Path, payload: Mapping[str, Any]) -> None:
    """Append one compact redacted JSON line to ``path`` safely."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Refusing to append provenance to symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = redact_value(dict(payload))
    line = json.dumps(safe_payload, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        raise


def _current_os_username() -> str:
    """Return current OS user name for hashing, never persisted raw."""
    if pwd is not None:
        try:
            name = pwd.getpwuid(os.getuid()).pw_name.strip()
            if name:
                return name
        except (KeyError, OSError):
            pass
    try:
        name = getpass.getuser().strip()
        if name:
            return name
    except Exception:
        pass
    for key in ("USER", "LOGNAME"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def hashed_approver_id(raw_user: str | None = None) -> str:
    """Return a redacted, non-PII approver identifier derived by hashing the OS user name."""
    user = (raw_user if raw_user is not None else _current_os_username()).strip()
    if not user:
        return "human:anonymous"
    digest = hashlib.sha256(f"ai-devpack-approver:{user}".encode("utf-8")).hexdigest()[:12]
    return f"human:{digest}"


def validate_checkpoint_id(checkpoint_id: str) -> str:
    """Validate and return canonical checkpoint identifier."""
    cleaned = str(checkpoint_id or "").strip().lower()
    if cleaned not in CHECKPOINT_SPECS:
        valid = ", ".join(sorted(CHECKPOINT_SPECS))
        raise ValueError(f"Unknown checkpoint '{checkpoint_id}' (expected one of: {valid})")
    return cleaned


def default_target_branch_for(
    protected_branches: Sequence[str] = DEFAULT_PROTECTED_BRANCHES,
) -> str:
    """Return the first concrete entry of ``protected_branches``, or ``'main'``."""
    for entry in protected_branches:
        cleaned = str(entry or "").strip()
        if cleaned and not any(ch in cleaned for ch in "*?["):
            return cleaned
    return DEFAULT_TARGET_BRANCH


def validate_pipeline_dict(raw_pipeline: Any, *, source: str = "project.toml") -> dict[str, Any]:
    """Validate a parsed ``[pipeline]`` TOML table and reject loosening overrides."""
    if not isinstance(raw_pipeline, Mapping):
        raise ValueError(f"[pipeline] must be a TOML table in {source}")

    raw_mode = raw_pipeline.get("mode", DEFAULT_MODE)
    if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in VALID_MODES:
        raise ValueError(
            f"Invalid [pipeline].mode {raw_mode!r} in {source} (expected 'hitl' or 'autonomous')"
        )
    mode = raw_mode.strip().lower()

    state_dir_explicit = "state_dir" in raw_pipeline
    if state_dir_explicit:
        raw_state_dir = raw_pipeline.get("state_dir")
        if not isinstance(raw_state_dir, str) or not raw_state_dir.strip():
            raise ValueError(f"[pipeline].state_dir must be a non-empty string in {source}")
        state_dir = raw_state_dir.strip()
    else:
        state_dir = ""

    if "protected_branches" in raw_pipeline and raw_pipeline["protected_branches"] is not None:
        raw_prot = raw_pipeline["protected_branches"]
        if (
            not isinstance(raw_prot, (list, tuple))
            or not raw_prot
            or any(not isinstance(item, str) or not item.strip() for item in raw_prot)
        ):
            raise ValueError(
                f"[pipeline].protected_branches must be a non-empty list of branch names/globs in {source}"
            )
        protected_branches = [str(item).strip() for item in raw_prot]
    else:
        protected_branches = list(DEFAULT_PROTECTED_BRANCHES)

    default_target = default_target_branch_for(protected_branches)
    if "target_branch" in raw_pipeline:
        raw_target = raw_pipeline.get("target_branch")
        if not isinstance(raw_target, str):
            raise ValueError(f"[pipeline].target_branch must be a string in {source}")
        target_branch = raw_target.strip()
    else:
        target_branch = default_target

    raw_checkpoints = raw_pipeline.get("checkpoints", {})
    if raw_checkpoints is None:
        raw_checkpoints = {}
    if not isinstance(raw_checkpoints, Mapping):
        raise ValueError(f"[pipeline.checkpoints] must be a TOML table in {source}")

    overrides: dict[str, str] = {}
    for raw_cid, raw_policy in raw_checkpoints.items():
        cid = validate_checkpoint_id(str(raw_cid))
        if not isinstance(raw_policy, str) or raw_policy.strip().lower() not in VALID_CHECKPOINT_OVERRIDES:
            raise ValueError(
                f"Invalid [pipeline.checkpoints].{cid} value {raw_policy!r} in {source} "
                "(expected 'auto' or 'human')"
            )
        policy = raw_policy.strip().lower()
        if policy == "auto":
            if cid in ALWAYS_HARD_STOP_CHECKPOINTS:
                raise ValueError(
                    f"Invalid [pipeline.checkpoints] in {source}: hard-stop checkpoint '{cid}' "
                    "cannot be set to 'auto' (human approval is always required)"
                )
            if mode == "hitl":
                raise ValueError(
                    f"Invalid [pipeline.checkpoints] in {source}: checkpoint '{cid}' cannot be set "
                    "to 'auto' when [pipeline].mode is 'hitl' (overrides may only make policy stricter)"
                )
        overrides[cid] = policy

    return {
        "mode": mode,
        "state_dir": state_dir,
        "state_dir_explicit": state_dir_explicit,
        "target_branch": target_branch,
        "protected_branches": protected_branches,
        "checkpoints": overrides,
    }


def resolve_pipeline_mode(
    config_mode: str,
    *,
    cli_mode: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve effective mode from config, ``DEVPACK_MODE``, and CLI ``--mode`` (tightening only)."""
    base = str(config_mode or DEFAULT_MODE).strip().lower()
    if base not in VALID_MODES:
        raise ValueError(f"Invalid config pipeline mode '{config_mode}' (expected 'hitl' or 'autonomous')")

    effective = base
    source_env = os.environ if env is None else env
    env_mode_raw = str(source_env.get("DEVPACK_MODE", "")).strip().lower()
    if env_mode_raw:
        if env_mode_raw not in VALID_MODES:
            raise ValueError(
                f"Invalid DEVPACK_MODE={env_mode_raw!r} (expected 'hitl' or 'autonomous')"
            )
        if MODE_STRICTNESS[env_mode_raw] < MODE_STRICTNESS[effective]:
            raise ValueError(
                f"DEVPACK_MODE='{env_mode_raw}' cannot loosen configured pipeline mode '{effective}'"
            )
        effective = env_mode_raw

    if cli_mode is not None and str(cli_mode).strip():
        cli_clean = str(cli_mode).strip().lower()
        if cli_clean not in VALID_MODES:
            raise ValueError(f"Invalid --mode {cli_mode!r} (expected 'hitl' or 'autonomous')")
        if MODE_STRICTNESS[cli_clean] < MODE_STRICTNESS[effective]:
            raise ValueError(
                f"--mode '{cli_clean}' cannot loosen pipeline mode '{effective}' "
                "(CLI/env overrides may only tighten autonomous -> hitl)"
            )
        effective = cli_clean

    return effective


def load_pipeline_config(
    workdir: Path | str | None = None,
    project_config_path: Path | str | None = None,
    *,
    cli_mode: str | None = None,
    env: Mapping[str, str] | None = None,
    trusted_project_bytes: bytes | None = None,
    override_mode: str | None = None,
    override_state_dir: Path | str | None = None,
    agent_worktree: Path | str | None = None,
    writable_mounts: Sequence[Path | str] = (),
    cli_target_branch: str | None = None,
    override_target_branch: str | None = None,
) -> PipelineConfig:
    """Load trusted ``[pipeline]`` config, apply CLI/env tightening, and validate ``state_dir``."""
    raw_doc: dict[str, Any] = {}
    source_label = "default"
    if trusted_project_bytes is not None:
        raw_doc = parse_toml_with_arrays(trusted_project_bytes.decode("utf-8", errors="replace"))
        source_label = "trusted-snapshot"
    else:
        cfg_path: Path | None = None
        if project_config_path is not None and str(project_config_path).strip():
            cfg_path = Path(project_config_path)
        elif workdir is not None:
            cfg_path = Path(workdir) / ".devpack" / "project.toml"
        if cfg_path is not None:
            if cfg_path.is_symlink():
                raise ValueError(f"Project config must not be a symlink: {cfg_path}")
            if cfg_path.is_file():
                raw_doc = parse_toml_with_arrays(cfg_path.read_text(encoding="utf-8"))
                source_label = str(cfg_path)

    if "pipeline" in raw_doc:
        parsed = validate_pipeline_dict(raw_doc["pipeline"], source=source_label)
    else:
        parsed = {
            "mode": DEFAULT_MODE,
            "state_dir": "",
            "state_dir_explicit": False,
            "target_branch": DEFAULT_TARGET_BRANCH,
            "protected_branches": list(DEFAULT_PROTECTED_BRANCHES),
            "checkpoints": {},
        }

    source_env = os.environ if env is None else env
    effective_mode = resolve_pipeline_mode(parsed["mode"], cli_mode=cli_mode, env=source_env)
    mode_configured = (
        "pipeline" in raw_doc
        or bool(str(source_env.get("DEVPACK_MODE", "")).strip())
        or (cli_mode is not None and bool(str(cli_mode).strip()))
    )
    if override_mode is not None:
        cleaned_override = str(override_mode).strip().lower()
        if cleaned_override not in VALID_MODES:
            raise ValueError(f"Invalid pipeline mode '{override_mode}' (expected 'hitl' or 'autonomous')")
        if not mode_configured or MODE_STRICTNESS[cleaned_override] > MODE_STRICTNESS[effective_mode]:
            effective_mode = cleaned_override

    state_dir_val = parsed["state_dir"]
    state_explicit = bool(parsed["state_dir_explicit"])
    env_state = str(source_env.get("DEVPACK_STATE_DIR", "")).strip()
    if override_state_dir is not None and str(override_state_dir).strip():
        state_dir_val = str(override_state_dir).strip()
        state_explicit = True
    elif env_state:
        state_dir_val = env_state
        state_explicit = True

    resolved_state = resolve_state_dir(
        state_dir_val or None,
        project_root=workdir,
        agent_worktree=agent_worktree,
        writable_mounts=writable_mounts,
        explicit=state_explicit,
        env=source_env,
    )

    if override_target_branch is not None:
        effective_target = str(override_target_branch).strip()
    elif cli_target_branch is not None:
        effective_target = str(cli_target_branch).strip()
    elif "DEVPACK_TARGET_BRANCH" in source_env:
        effective_target = str(source_env.get("DEVPACK_TARGET_BRANCH", "")).strip()
    else:
        effective_target = str(parsed.get("target_branch", DEFAULT_TARGET_BRANCH)).strip()

    return PipelineConfig(
        mode=effective_mode,
        state_dir=str(resolved_state),
        state_dir_explicit=state_explicit,
        target_branch=effective_target,
        protected_branches=tuple(parsed.get("protected_branches", DEFAULT_PROTECTED_BRANCHES)),
        checkpoints=dict(parsed["checkpoints"]),
    )


def _check_no_symlinks_under(path: Path, stop_at: Path | None = None) -> None:
    """Refuse symlinks along ``path`` and its components down from ``stop_at`` (or root)."""
    cur = path
    checked = 0
    while checked < 32:
        if cur.is_symlink():
            raise ValueError(f"Checkpoint state_dir path must not be or traverse a symlink: {cur}")
        if stop_at is not None and cur == stop_at:
            break
        if cur.parent == cur:
            break
        cur = cur.parent
        checked += 1


def default_state_dir(
    project_root: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the default external state directory ``$XDG_STATE_HOME/devpack/<project-hash>``."""
    base_root = Path(project_root).resolve() if project_root is not None else Path.cwd().resolve()
    project_hash = hashlib.sha256(str(base_root).encode("utf-8")).hexdigest()[:16]
    source_env = os.environ if env is None else env
    xdg_raw = str(source_env.get("XDG_STATE_HOME", "")).strip()
    if xdg_raw:
        xdg_base = Path(xdg_raw)
        if not xdg_base.is_absolute():
            raise ValueError(f"XDG_STATE_HOME must be an absolute path when set, got {xdg_raw!r}")
    else:
        home_raw = str(source_env.get("HOME", "")).strip()
        home_path = Path(home_raw) if home_raw else Path.home()
        xdg_base = home_path / ".local" / "state"
    return xdg_base / "devpack" / project_hash


def resolve_state_dir(
    configured_state_dir: Path | str | None = None,
    *,
    project_root: Path | str | None = None,
    agent_worktree: Path | str | None = None,
    writable_mounts: Sequence[Path | str] = (),
    explicit: bool = False,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve checkpoint state directory and refuse paths inside a worktree or writable mount."""
    del explicit  # Containment inside worktree/writable mounts is always refused.
    base_root = Path(project_root).resolve() if project_root is not None else Path.cwd().resolve()
    raw = str(configured_state_dir).strip() if configured_state_dir is not None else ""
    if not raw:
        cand = default_state_dir(base_root, env=env)
    else:
        cand = Path(raw)
        if not cand.is_absolute():
            cand = base_root / cand
    _check_no_symlinks_under(cand, base_root if cand.is_relative_to(base_root) else None)
    resolved = cand.resolve()

    if agent_worktree is not None:
        aw_res = Path(agent_worktree).resolve()
        if resolved == aw_res or resolved.is_relative_to(aw_res):
            raise ValueError(
                f"Checkpoint state_dir '{resolved}' must live outside agent worktree '{aw_res}'"
            )

    for wm in writable_mounts:
        if wm is None or not str(wm).strip():
            continue
        wm_res = Path(wm).resolve()
        if resolved == wm_res or resolved.is_relative_to(wm_res):
            raise ValueError(
                f"Checkpoint state_dir '{resolved}' must live outside writable mount '{wm_res}'"
            )

    if project_root is not None:
        if resolved == base_root or resolved.is_relative_to(base_root):
            raise ValueError(
                f"Checkpoint state_dir '{resolved}' must live outside worktree '{base_root}'"
            )

    return resolved


def external_state_dir_for_runs_root(
    workdir: Path | str,
    runs_root: Path | str | None,
) -> Path | None:
    """Return a checkpoint state directory outside ``workdir`` derived from an external ``runs_root``."""
    if runs_root is None:
        return None
    wd_res = Path(workdir).resolve()
    rr_res = Path(runs_root).resolve()
    if rr_res == wd_res or rr_res.is_relative_to(wd_res):
        return None
    digest = hashlib.sha256(str(wd_res).encode("utf-8")).hexdigest()[:8]
    return rr_res.with_name(f".{rr_res.name}-state-{digest}")


def is_protected_target_branch(
    target_branch: str | None,
    *,
    protected_branches: Sequence[str] = DEFAULT_PROTECTED_BRANCHES,
) -> tuple[bool, str]:
    """Return ``(is_protected, branch_name)`` for a merge target, failing closed when unknown."""
    cleaned = str(target_branch or "").strip()
    if not cleaned or cleaned.upper() in ("HEAD", "UNKNOWN", "FETCH_HEAD"):
        return True, cleaned
    candidates = [cleaned]
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if cleaned.startswith(prefix):
            stripped = cleaned[len(prefix):].strip()
            if stripped:
                candidates.append(stripped)
    patterns = tuple(str(p).strip() for p in protected_branches if str(p).strip()) or DEFAULT_PROTECTED_BRANCHES
    for cand in candidates:
        if cand in _PROTECTED_BRANCH_NAMES or cand.startswith(_PROTECTED_BRANCH_PREFIXES):
            return True, cleaned
        for pat in patterns:
            if pat.endswith("/"):
                if cand.startswith(pat):
                    return True, cleaned
            elif fnmatch.fnmatchcase(cand, pat):
                return True, cleaned
    return False, cleaned


def is_hard_stop(checkpoint_id: str, metadata: Mapping[str, Any] | None = None) -> bool:
    """Return True when ``checkpoint_id`` with ``metadata`` always requires human approval."""
    cid = validate_checkpoint_id(checkpoint_id)
    if cid in ALWAYS_HARD_STOP_CHECKPOINTS:
        return True
    meta = dict(metadata or {})
    if cid == "pre-merge":
        if bool(meta.get("protected_branch", False)):
            return True
        prot_list = meta.get("protected_branches")
        prot_patterns: Sequence[str] = (
            tuple(str(x) for x in prot_list)
            if isinstance(prot_list, (list, tuple)) and prot_list
            else DEFAULT_PROTECTED_BRANCHES
        )
        if "target_branch" in meta:
            prot, _ = is_protected_target_branch(meta.get("target_branch"), protected_branches=prot_patterns)
            return prot
        if meta.get("protected_branch") is False and str(meta.get("branch", "")).strip():
            prot, _ = is_protected_target_branch(meta.get("branch"), protected_branches=prot_patterns)
            return prot
        # Fail closed when merge target branch is unknown.
        return True
    if cid == "deviation":
        if bool(meta.get("safety_relevant", False)) or bool(meta.get("security_relevant", False)):
            return True
        asil = str(meta.get("asil", "")).strip().upper()
        if asil in _HIGH_ASIL_VALUES:
            return True
        return False
    if cid == "selinux-manifest":
        if bool(meta.get("release_build", False)):
            return True
        if str(meta.get("build_type", "")).strip().lower() == "release":
            return True
        return False
    return False


def is_protected_branch(
    repo_root: Path | str,
    *,
    target_branch: str | None = None,
    protected_branches: Sequence[str] = DEFAULT_PROTECTED_BRANCHES,
) -> tuple[bool, str]:
    """Detect whether ``target_branch`` (or ``repo_root`` HEAD if omitted) is protected; fail closed if unknown."""
    if target_branch is not None:
        return is_protected_target_branch(target_branch, protected_branches=protected_branches)
    try:
        proc = run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            repo_root=Path(repo_root),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, GitSecurityError):
        return True, ""
    if proc.returncode != 0:
        return True, ""
    branch = proc.stdout.strip()
    return is_protected_target_branch(branch, protected_branches=protected_branches)


def _inspect_evidence_file(checkpoint_id: str, path: Path) -> tuple[bool, bool, str, str]:
    """Inspect one gate/lint evidence file.

    Returns ``(valid_green, saw_gate_signal, sha256, detail)``.
    """
    if path.is_symlink() or not path.is_file():
        return False, False, "", f"missing or symlinked evidence file: {path}"
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, False, digest, f"malformed JSON evidence {path.name}: {exc}"

        # brief_lint JSON output is a list of {rule, severity, line, message}
        if isinstance(data, list):
            errors = [
                item for item in data
                if isinstance(item, Mapping) and str(item.get("severity", "")).lower() == "error"
            ]
            if errors:
                return False, True, digest, f"brief_lint reported {len(errors)} error finding(s)"
            return True, True, digest, "brief_lint clean (0 errors)"

        if not isinstance(data, Mapping):
            return False, False, digest, f"unexpected JSON evidence root in {path.name}"

        if "brief_lint" in data and isinstance(data["brief_lint"], list):
            errors = [
                item for item in data["brief_lint"]
                if isinstance(item, Mapping) and str(item.get("severity", "")).lower() == "error"
            ]
            if errors:
                return False, True, digest, f"brief_lint reported {len(errors)} error finding(s)"
            if "error_count" in data and int(data["error_count"]) > 0:
                return False, True, digest, f"brief_lint error_count={data['error_count']}"
            return True, True, digest, "brief_lint clean (0 errors)"

        if "gate_status" in data:
            gs = str(data.get("gate_status", "")).strip().upper()
            if gs in ("GATE_OK", "PASS", "OK"):
                return True, True, digest, f"gate_status={gs}"
            return False, True, digest, f"gate_status={gs or 'MISSING'}"

        if "gate" in data:
            g = str(data.get("gate", "")).strip().lower()
            if g != "ok":
                return False, True, digest, f"gate={g}"
            scope = str(data.get("scope", "ok")).strip().lower()
            if scope != "ok":
                return False, True, digest, f"scope={scope}"
            return True, True, digest, "gate=ok"

        if "status" in data:
            st = str(data.get("status", "")).strip().upper()
            if st in ("GATE_OK", "PASS", "OK", "APPROVED"):
                return True, True, digest, f"status={st}"
            if st in ("GATE_FAIL", "FAIL", "REFUSED", "ERROR", "TIMEOUT", "HARNESS_ERROR", "ESCALATE_TO_HUMAN", "REJECTED"):
                return False, True, digest, f"status={st}"

        if "passed" in data:
            passed = bool(data["passed"])
            return passed, True, digest, f"passed={passed}"

        if "outcome" in data:
            out = str(data.get("outcome", "")).strip().lower()
            if out in ("pass", "ok", "approved"):
                return True, True, digest, f"outcome={out}"
            return False, True, digest, f"outcome={out}"

        return False, False, digest, f"no gate verdict fields in {path.name}"

    text = raw_bytes.decode("utf-8", errors="replace")
    if not text.strip():
        return False, False, digest, f"empty evidence file: {path.name}"
    for marker in _FAILURE_TEXT_MARKERS:
        if marker in text:
            return False, True, digest, f"failure marker '{marker}' in {path.name}"
    if "GATE_OK" in text or "VERDICT: APPROVE" in text:
        return True, True, digest, f"passing receipt text in {path.name}"
    return False, False, digest, f"no passing gate marker in {path.name}"


def verify_gate_evidence(
    checkpoint_id: str,
    evidence_paths: Sequence[Path | str],
    *,
    gate_passed: bool | None = None,
) -> tuple[bool, list[dict[str, str]], str]:
    """Verify whether stage gate evidence is green (fail-closed)."""
    if gate_passed is False:
        receipts: list[dict[str, str]] = []
        for raw in evidence_paths:
            p = Path(raw)
            if p.is_file() and not p.is_symlink():
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                receipts.append({"path": str(p), "sha256": digest})
        return False, receipts, "caller reported gate_passed=False"

    receipts = []
    saw_green = False
    reasons: list[str] = []

    for raw in evidence_paths:
        p = Path(raw)
        ok, has_signal, digest, detail = _inspect_evidence_file(checkpoint_id, p)
        receipts.append({"path": str(p), "sha256": digest})
        if has_signal and not ok:
            return False, receipts, detail
        if not ok and not has_signal and p.is_symlink():
            return False, receipts, detail
        if not ok and not p.exists():
            return False, receipts, detail
        if ok and has_signal:
            saw_green = True
        elif detail:
            reasons.append(detail)

    if saw_green:
        return True, receipts, "gate evidence green"
    if gate_passed is True and receipts:
        return True, receipts, "trusted gate_passed=True with evidence"
    if gate_passed is True and not receipts:
        return False, receipts, "cannot auto-approve without gate receipt evidence"
    return False, receipts, "; ".join(reasons) if reasons else "no green gate receipt found"


def _append_provenance(
    state_dir: Path,
    record: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
    commit_sha: str | None = None,
) -> None:
    """Append a checkpoint decision to ``<state_dir>/approvals/provenance.jsonl`` and git notes."""
    prov_entry = {
        "approval_id": record.get("id", ""),
        "checkpoint": record.get("checkpoint", ""),
        "kind": record.get("kind", ""),
        "status": record.get("status", ""),
        "mode": record.get("mode", ""),
        "hard_stop": bool(record.get("hard_stop", False)),
        "subject": record.get("subject", ""),
        "decided_by": record.get("decided_by", ""),
        "decided_at": record.get("decided_at", ""),
        "created": record.get("created", ""),
        "note": record.get("note", ""),
        "gate_receipts": record.get("gate_receipts", []),
        "commit_sha": commit_sha or "",
    }
    prov_path = state_dir / "approvals" / "provenance.jsonl"
    _append_jsonl_line(prov_path, prov_entry)

    if repo_root is not None and (Path(repo_root) / ".git").exists():
        try:
            target_ref = commit_sha or "HEAD"
            rev_proc = run_git(
                ["rev-parse", "--verify", target_ref],
                repo_root=Path(repo_root),
                timeout=10,
                check=False,
            )
            if rev_proc.returncode == 0 and rev_proc.stdout.strip():
                resolved_sha = rev_proc.stdout.strip()
                prov_entry["commit_sha"] = resolved_sha
                note_line = json.dumps(redact_value(prov_entry), separators=(",", ":"))
                run_git(
                    [
                        "-c", "user.name=ai-devpack",
                        "-c", "user.email=devpack@example.invalid",
                        "notes", "--ref=refs/notes/ai-provenance",
                        "append", "-m", note_line, resolved_sha,
                    ],
                    repo_root=Path(repo_root),
                    timeout=10,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError, GitSecurityError):
            pass


def _decision_from_record(record: Mapping[str, Any], record_path: Path) -> Decision:
    """Construct a ``Decision`` dataclass from a persisted approval record dictionary."""
    receipts = tuple(
        {"path": str(item.get("path", "")), "sha256": str(item.get("sha256", ""))}
        for item in record.get("gate_receipts", [])
        if isinstance(item, Mapping)
    )
    return Decision(
        approval_id=str(record.get("id", "")),
        checkpoint_id=str(record.get("checkpoint", "")),
        status=str(record.get("status", PENDING)),
        kind=str(record.get("kind", "blocking")),
        hard_stop=bool(record.get("hard_stop", False)),
        decided_by=str(record.get("decided_by", "")),
        record_path=record_path,
        subject=str(record.get("subject", "")),
        summary=str(record.get("summary", "")),
        evidence_paths=tuple(str(x) for x in record.get("evidence", [])),
        note=str(record.get("note", "")),
        created=str(record.get("created", "")),
        decided_at=str(record.get("decided_at", "")),
        mode=str(record.get("mode", DEFAULT_MODE)),
        gate_receipts=receipts,
    )


def request(
    checkpoint_id: str,
    subject: str,
    evidence_paths: Sequence[Path | str] = (),
    summary: str = "",
    *,
    mode: str | None = None,
    pipeline_config: PipelineConfig | Mapping[str, Any] | None = None,
    state_dir: Path | str | None = None,
    project_root: Path | str | None = None,
    agent_worktree: Path | str | None = None,
    writable_mounts: Sequence[Path | str] = (),
    requested_by: str = "runner",
    gate_passed: bool | None = None,
    metadata: Mapping[str, Any] | None = None,
    repo_root: Path | str | None = None,
    run_dir: Path | str | None = None,
    commit_sha: str | None = None,
) -> Decision:
    """Evaluate a pipeline checkpoint and persist either a ``pending`` or ``decided`` record."""
    cid = validate_checkpoint_id(checkpoint_id)
    spec = CHECKPOINT_SPECS[cid]
    kind = str(spec["kind"])

    eff_project_root = Path(project_root).resolve() if project_root is not None else (
        Path(repo_root).resolve() if repo_root is not None else None
    )
    cfg_protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    if isinstance(pipeline_config, PipelineConfig):
        cfg_mode = pipeline_config.mode
        cfg_overrides = dict(pipeline_config.checkpoints)
        cfg_state_dir: Path | str | None = pipeline_config.state_dir
        cfg_explicit = pipeline_config.state_dir_explicit
        cfg_protected_branches = pipeline_config.protected_branches
    elif isinstance(pipeline_config, Mapping):
        cfg_mode = str(pipeline_config.get("mode", DEFAULT_MODE)).strip().lower()
        cfg_overrides = dict(pipeline_config.get("checkpoints", {}) or {})
        cfg_state_dir = pipeline_config.get("state_dir")
        cfg_explicit = bool(pipeline_config.get("state_dir_explicit", False))
        raw_pb = pipeline_config.get("protected_branches")
        if isinstance(raw_pb, (list, tuple)) and raw_pb:
            cfg_protected_branches = tuple(str(x) for x in raw_pb)
    elif eff_project_root is not None:
        loaded_cfg = load_pipeline_config(
            eff_project_root,
            override_mode=mode,
            override_state_dir=state_dir,
            agent_worktree=agent_worktree,
            writable_mounts=writable_mounts,
        )
        cfg_mode = loaded_cfg.mode
        cfg_overrides = loaded_cfg.checkpoints
        cfg_state_dir = loaded_cfg.state_dir
        cfg_explicit = loaded_cfg.state_dir_explicit
        cfg_protected_branches = loaded_cfg.protected_branches
    else:
        cfg_mode = DEFAULT_MODE
        cfg_overrides = {}
        cfg_state_dir = None
        cfg_explicit = False

    if mode is not None:
        eff_mode = str(mode).strip().lower()
        if eff_mode not in VALID_MODES:
            raise ValueError(f"Invalid pipeline mode '{mode}' (expected 'hitl' or 'autonomous')")
        if (pipeline_config is not None or eff_project_root is not None) and (
            MODE_STRICTNESS[cfg_mode] > MODE_STRICTNESS[eff_mode]
        ):
            eff_mode = cfg_mode
    else:
        eff_mode = cfg_mode

    if state_dir is not None and str(state_dir).strip():
        resolved_state = resolve_state_dir(
            state_dir,
            project_root=eff_project_root,
            agent_worktree=agent_worktree,
            writable_mounts=writable_mounts,
            explicit=True,
        )
    else:
        resolved_state = resolve_state_dir(
            cfg_state_dir,
            project_root=eff_project_root,
            agent_worktree=agent_worktree,
            writable_mounts=writable_mounts,
            explicit=cfg_explicit,
        )

    eff_meta = dict(metadata or {})
    if cid == "pre-merge" and "protected_branches" not in eff_meta:
        eff_meta["protected_branches"] = list(cfg_protected_branches)
    hard_stop = is_hard_stop(cid, metadata=eff_meta)
    checkpoint_override = cfg_overrides.get(cid, "")
    requires_human = (eff_mode == "hitl") or hard_stop or (checkpoint_override == "human")

    ev_list = [str(p) for p in evidence_paths]
    green, receipts, gate_detail = verify_gate_evidence(cid, evidence_paths, gate_passed=gate_passed)

    approval_id = uuid.uuid4().hex
    now_iso = _now_utc_iso()
    approvals_root = resolved_state / "approvals"
    safe_meta = redact_value(dict(metadata or {}))
    safe_summary = redact_sensitive_text(str(summary or ""))
    safe_subject = redact_sensitive_text(str(subject or ""))

    base_record: dict[str, Any] = {
        "id": approval_id,
        "checkpoint": cid,
        "kind": kind,
        "hard_stop": hard_stop,
        "mode": eff_mode,
        "subject": safe_subject,
        "summary": safe_summary,
        "evidence": ev_list,
        "gate_receipts": receipts,
        "gate_detail": gate_detail,
        "created": now_iso,
        "requested_by": redact_sensitive_text(str(requested_by or "runner")),
        "run_dir": redact_sensitive_text(str(run_dir)) if run_dir else "",
        "metadata": safe_meta,
    }

    if not requires_human and green:
        base_record["status"] = APPROVED
        base_record["decided_by"] = "autonomous-policy"
        base_record["decided_at"] = now_iso
        base_record["note"] = "Auto-approved by autonomous policy with green gate evidence."
        record_path = approvals_root / "decided" / f"{approval_id}.json"
        _atomic_write_json(record_path, base_record)
        _append_provenance(
            resolved_state,
            base_record,
            repo_root=Path(repo_root) if repo_root else eff_project_root,
            commit_sha=commit_sha,
        )
        return _decision_from_record(base_record, record_path)

    reason_note = ""
    if hard_stop:
        reason_note = "Hard-stop checkpoint requires human approval."
    elif checkpoint_override == "human":
        reason_note = "Checkpoint configured for human approval."
    elif eff_mode == "hitl":
        reason_note = "HITL mode requires human approval."
    elif not green:
        reason_note = f"Gate evidence not green ({gate_detail}); held for human approval."

    base_record["status"] = PENDING
    base_record["decided_by"] = ""
    base_record["decided_at"] = ""
    base_record["note"] = reason_note
    record_path = approvals_root / "pending" / f"{approval_id}.json"
    _atomic_write_json(record_path, base_record)
    return _decision_from_record(base_record, record_path)


def _validate_approval_id(approval_id: str) -> str:
    """Validate approval identifier against path traversal."""
    cleaned = str(approval_id or "").strip()
    if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned or not _APPROVAL_ID_RE.match(cleaned):
        raise ValueError(f"Invalid approval id: {approval_id!r}")
    return cleaned


def _read_record(path: Path) -> dict[str, Any]:
    """Read and validate one approval JSON file without following symlinks."""
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Approval record not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Malformed approval record: {path}")
    return data


def list_approvals(
    state_dir: Path | str,
    *,
    status: str = "all",
    checkpoint_id: str = "",
) -> list[dict[str, Any]]:
    """List approval records under ``<state_dir>/approvals/``."""
    root = Path(state_dir) / "approvals"
    buckets = ("pending", "decided") if status == "all" else (status,)
    records: list[dict[str, Any]] = []
    for bucket in buckets:
        if bucket not in ("pending", "decided"):
            raise ValueError(f"Invalid approval status filter: {bucket!r}")
        bdir = root / bucket
        if not bdir.is_dir() or bdir.is_symlink():
            continue
        for item in sorted(bdir.glob("*.json")):
            if item.name.startswith(".") or item.is_symlink():
                continue
            try:
                rec = _read_record(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if checkpoint_id and rec.get("checkpoint") != checkpoint_id:
                continue
            rec["_bucket"] = bucket
            rec["_path"] = str(item)
            records.append(rec)
    records.sort(key=lambda r: (str(r.get("created", "")), str(r.get("id", ""))))
    return records


def show_approval(state_dir: Path | str, approval_id: str) -> tuple[dict[str, Any], Path]:
    """Load an approval record by ID from either ``pending/`` or ``decided/``."""
    aid = _validate_approval_id(approval_id)
    root = Path(state_dir) / "approvals"
    for bucket in ("pending", "decided"):
        cand = root / bucket / f"{aid}.json"
        if cand.is_file() or cand.is_symlink():
            return _read_record(cand), cand
    raise FileNotFoundError(f"Approval record not found: {aid}")


def find_pending_for_subject(
    state_dir: Path | str,
    checkpoint_id: str,
    subject: str,
) -> tuple[dict[str, Any], Path] | None:
    """Find the most recent pending approval matching ``checkpoint_id`` and ``subject``."""
    pending_dir = Path(state_dir) / "approvals" / "pending"
    if not pending_dir.is_dir() or pending_dir.is_symlink():
        return None
    matches: list[tuple[str, dict[str, Any], Path]] = []
    for item in sorted(pending_dir.glob("*.json")):
        if item.name.startswith(".") or item.is_symlink():
            continue
        try:
            rec = _read_record(item)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if rec.get("checkpoint") == checkpoint_id and rec.get("subject") == subject:
            matches.append((str(rec.get("created", "")), rec, item))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0])
    _, rec, path = matches[-1]
    return rec, path


def decide_approval(
    state_dir: Path | str,
    approval_id: str,
    *,
    decision_status: str,
    note: str = "",
    repo_root: Path | str | None = None,
    approver: str | None = None,
    sync_memory: bool = True,
) -> Decision:
    """Approve or reject a pending checkpoint and move its record to ``decided/``."""
    aid = _validate_approval_id(approval_id)
    norm_status = str(decision_status or "").strip().upper()
    if norm_status not in (APPROVED, REJECTED):
        raise ValueError(f"Invalid decision status '{decision_status}' (expected APPROVED or REJECTED)")
    if norm_status == REJECTED and not str(note or "").strip():
        raise ValueError("Rejecting an approval requires a non-empty --note")

    resolved_state = Path(state_dir).resolve()
    pending_path = resolved_state / "approvals" / "pending" / f"{aid}.json"
    decided_path = resolved_state / "approvals" / "decided" / f"{aid}.json"

    if decided_path.exists():
        raise ValueError(f"Approval '{aid}' has already been decided")
    if not pending_path.is_file():
        raise FileNotFoundError(f"Pending approval '{aid}' not found")

    record = _read_record(pending_path)
    decided_by = approver if approver is not None else hashed_approver_id()
    now_iso = _now_utc_iso()

    record["status"] = norm_status
    record["decided_by"] = redact_sensitive_text(decided_by)
    record["decided_at"] = now_iso
    record["note"] = redact_sensitive_text(str(note or "").strip())

    _atomic_write_json(decided_path, record)
    pending_path.unlink(missing_ok=True)

    if sync_memory and record.get("checkpoint") == "memory-decision":
        meta = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        mem_root_raw = str(meta.get("memory_root", "")).strip()
        mem_id = str(meta.get("memory_id", "") or record.get("subject", "")).strip()
        if mem_root_raw and mem_id:
            try:
                try:
                    from runner import memory as memory_mod  # noqa: E402
                except ModuleNotFoundError:  # pragma: no cover
                    import memory as memory_mod  # type: ignore[no-redef] # noqa: E402
                mem_root = Path(mem_root_raw)
                if mem_root.is_dir():
                    if norm_status == APPROVED:
                        memory_mod.approve_memory_entry(mem_root, mem_id, _sync_checkpoint=False)
                    else:
                        memory_mod.expire_memory_entry(mem_root, mem_id)
            except Exception:
                pass

    eff_repo = Path(repo_root).resolve() if repo_root is not None else None
    _append_provenance(resolved_state, record, repo_root=eff_repo)
    return _decision_from_record(record, decided_path)


def register_deviation(
    deviation_id: str,
    summary: str,
    evidence_paths: Sequence[Path | str] = (),
    *,
    safety_relevant: bool = False,
    security_relevant: bool = False,
    asil: str = "",
    files: Sequence[str] = (),
    rules: Sequence[str] = (),
    mode: str | None = None,
    pipeline_config: PipelineConfig | Mapping[str, Any] | None = None,
    state_dir: Path | str | None = None,
    project_root: Path | str | None = None,
    agent_worktree: Path | str | None = None,
    gate_passed: bool | None = None,
    repo_root: Path | str | None = None,
) -> Decision:
    """Evaluate a deviation checkpoint (hard-stop when safety-relevant ASIL >= B or security-relevant)."""
    metadata = {
        "deviation_id": deviation_id,
        "safety_relevant": bool(safety_relevant),
        "security_relevant": bool(security_relevant),
        "asil": str(asil or "").strip(),
        "files": list(files),
        "rules": list(rules),
    }
    return request(
        "deviation",
        deviation_id,
        evidence_paths,
        summary,
        mode=mode,
        pipeline_config=pipeline_config,
        state_dir=state_dir,
        project_root=project_root,
        agent_worktree=agent_worktree,
        requested_by="deviation-registration",
        gate_passed=gate_passed,
        metadata=metadata,
        repo_root=repo_root,
    )


def save_resume_state(state_dir: Path | str, approval_id: str, payload: Mapping[str, Any]) -> Path:
    """Persist workflow continuation metadata for ``--resume <approval_id>``."""
    aid = _validate_approval_id(approval_id)
    resume_path = Path(state_dir).resolve() / "runs" / f"{aid}.json"
    _atomic_write_json(resume_path, payload, redact=False)
    return resume_path


def load_resume_state(state_dir: Path | str, approval_id: str) -> dict[str, Any]:
    """Load saved workflow continuation metadata for ``--resume <approval_id>``."""
    aid = _validate_approval_id(approval_id)
    resume_path = Path(state_dir).resolve() / "runs" / f"{aid}.json"
    return _read_record(resume_path)


def verify_brief_hash(brief_path: Path | str, expected_sha256: str) -> bytes:
    """Read ``brief_path`` without following symlinks and verify its SHA-256 matches ``expected_sha256``."""
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise ValueError("Resume state is missing brief_sha256")
    path = Path(brief_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing resume: brief file missing or symlinked: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(
            f"Refusing resume: brief '{path.name}' changed since checkpoint approval "
            f"(expected {expected[:12]}, got {actual[:12]})"
        )
    return data


def consume_resume_approval(
    state_dir: Path | str,
    approval_id: str,
    *,
    expected_checkpoint: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically verify and consume an ``APPROVED`` checkpoint once for resume."""
    aid = _validate_approval_id(approval_id)
    resolved_state = Path(state_dir).resolve()
    rec, rec_path = show_approval(resolved_state, aid)
    status = str(rec.get("status", "")).upper()
    if status != APPROVED:
        raise ValueError(f"Approval '{aid}' is not APPROVED (status={status})")
    checkpoint_id = str(rec.get("checkpoint", ""))
    if expected_checkpoint and checkpoint_id != expected_checkpoint:
        raise ValueError(
            f"Approval '{aid}' is for checkpoint '{checkpoint_id}', expected '{expected_checkpoint}'"
        )
    saved = load_resume_state(resolved_state, aid)
    saved_cp = str(saved.get("checkpoint_id", "")).strip()
    if saved_cp and checkpoint_id and saved_cp != checkpoint_id:
        raise ValueError(
            f"Resume state checkpoint '{saved_cp}' does not match approval '{checkpoint_id}'"
        )

    consumed_marker = resolved_state / "runs" / f"{aid}.consumed"
    if bool(rec.get("consumed", False)) or bool(saved.get("consumed", False)) or consumed_marker.exists() or consumed_marker.is_symlink():
        raise ValueError(f"Approval '{aid}' has already been consumed")
    for parent in (consumed_marker.parent, consumed_marker.parent.parent):
        if parent.is_symlink():
            raise ValueError(f"Refusing to consume approval inside symlinked directory: {parent}")
    consumed_marker.parent.mkdir(parents=True, exist_ok=True)
    now_iso = _now_utc_iso()
    try:
        fd = os.open(consumed_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError(f"Approval '{aid}' has already been consumed") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"id": aid, "checkpoint": checkpoint_id, "consumed_at": now_iso}, handle)
            handle.write("\n")
    except Exception:
        consumed_marker.unlink(missing_ok=True)
        raise

    rec["consumed"] = True
    rec["consumed_at"] = now_iso
    _atomic_write_json(rec_path, rec)
    saved["consumed"] = True
    saved["consumed_at"] = now_iso
    save_resume_state(resolved_state, aid, saved)
    return rec, saved
