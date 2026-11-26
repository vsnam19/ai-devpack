"""Map Pack artifacts to redacted dashboard snapshots."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import sys
import time

from runner.checkpoints import BLOCKING_CHECKPOINTS, CHECKPOINT_SPECS, TERMINAL_CHECKPOINTS
from runner.memory import scope_dir_name
from runner.redaction import Redactor

from .events import Stream
from .files import (
    Controls, DeferredRead, Files, JSON_LIMIT, OversizedArtifact, TICK_LIMIT, array, clean,
    identifier, mapping,
)

FILTER_HELP = (
    'Filter with space-separated AND terms. Keys are backend, state, role, agent, model. '
    'Use key=value or a bare literal matched against id, role, agent and model '
    'case-insensitively. Use double quotes for values with spaces.'
)
FILTER_KEYS = {"backend", "state", "role", "agent", "model"}
STEM = re.compile(r"(\d{2,})-([A-Za-z0-9_.-]+)\.(meta\.json|prompt\.md|jsonl)")
GATE = re.compile(r"step_(\d{2,})_gate\.json")
GATE_TEXT = re.compile(r"(\d{2,})-gate\.txt")
QUEUE = re.compile(r"(.+)\.(request|response)\.json")
OUTCOMES = {
    "running": "running", "awaiting_approval": "waiting for approval", "rejected": "rejected",
    "refused": "refused", "fail": "failed", "timeout": "failed", "pass": "succeeded",
    "APPROVED": "succeeded", "ESCALATE_TO_HUMAN": "failed", "git_metadata_tampered": "failed",
}
TERMINAL = {"succeeded", "failed", "refused", "rejected"}
APPROVE_CLI = Path(__file__).resolve().parents[1] / "devpack_approve.py"
READ_ERRORS = (OSError, ValueError, RecursionError)


@dataclass(frozen=True)
class Settings:
    workdir: Path
    runs: Path
    state: Path
    project_config: Path | None = None
    log_roots: tuple[Path, ...] = ()
    evidence_roots: tuple[Path, ...] = ()
    models: tuple[tuple[str, str], ...] = ()
    history_limit: int = 100
    stream_poll_s: int = 1
    active_poll_s: int = 3
    discovery_poll_s: int = 5
    quiet_threshold_s: int = 300


def terms(query):
    result = []
    for token in shlex.split(query):
        key, sep, value = token.partition("=")
        if not sep:
            value, key = key, ""
        elif key not in FILTER_KEYS or not value:
            raise ValueError("invalid filter term")
        result.append((key, value.casefold()))
    return result


def matches(job, query):
    def value_of(field):
        return str(job.get(field) or "").casefold()

    return all(
        value == value_of(key) if key else any(
            value in value_of(field) for field in ("id", "role", "agent", "model")
        )
        for key, value in query
    )


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def number(value, *, zero=True):
    if type(value) in (int, float) and 0 <= value < math.inf and (zero or value > 0):
        return value
    return None


def basename(value):
    if isinstance(value, str):
        name = Path(value).name
        if identifier(name):
            return name
    return None


def measured(record, aliases, direction=None):
    allow_zero = (
        type(record.get("schema_version")) is int and record["schema_version"] > 0
        and mapping(record.get("usage_provenance")).get(aliases[0]) == "measured"
    )
    values = [record.get(key) for key in aliases]
    if direction:
        values.append(mapping(record.get("tokens")).get(direction))
    return next((value for value in values if number(value, zero=allow_zero) is not None), None)


def usage(record):
    return {
        "cost_usd": measured(record, ("cost_usd", "total_cost_usd", "cost")),
        "tokens_in": measured(record, ("tokens_in", "input_tokens", "in_tokens"), "in"),
        "tokens_out": measured(record, ("tokens_out", "output_tokens", "out_tokens"), "out"),
    }


class Reader:
    def __init__(self, settings, clock=time.time, monotonic=time.monotonic):
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self.files = Files(settings.runs, settings.state, *settings.log_roots,
                           *settings.evidence_roots, settings.workdir)
        self.redactor = Redactor()
        self.models = dict(settings.models)
        self.runs = {}
        self.root_stamp = None
        self.run_health = "missing"
        self.ids = []
        self.deferred = set()
        self.history_cursor = 0
        self.discovery_at = -math.inf
        self.active_at = -math.inf
        self.approvals_at = -math.inf
        self.approval_cache = {}
        self.approval_files = {}
        self.continuation_files = {}
        self.approval_health = "missing"
        self.queue_at = -math.inf
        self.queue_cache = {}
        self.queue_files = {}
        self.queue_health = "missing"
        self.stream_key = None
        self.stream = None
        self.activity = {}
        self.gate_key = None
        self.gate_snapshot = [], []

    def text(self, value, limit=1600):
        return clean(self.redactor, value, limit)

    def safe_id(self, value):
        return value if identifier(value) and self.text(value) == value else None

    def select(self, record, *keys):
        """Copy allowlisted fields with bounded text and finite scalar values."""
        result = {}
        for key in keys:
            value = record.get(key)
            if isinstance(value, str):
                result[key] = self.text(value)
            elif type(value) is bool or type(value) is int or (
                type(value) is float and math.isfinite(value)
            ):
                result[key] = value
        return result

    def findings(self, value):
        return [
            self.select(item, "severity", "rule", "rule_id", "message", "path", "line", "detail",
                        "name", "status", "returncode", "duration_s", "error")
            for item in array(value)[:50] if isinstance(item, dict)
        ]

    def optional(self, path):
        try:
            return self.files.json(path)
        except FileNotFoundError:
            return {}

    def excerpt(self, path):
        try:
            data = self.files.read(path, JSON_LIMIT)
            return self.text(data.decode("utf-8", errors="replace"))
        except READ_ERRORS:
            return None

    def response(self, record):
        return {
            **self.select(record, "status", "error", "served_model_id", "gate", "duration_s"),
            **usage(record),
            "target": self.safe_id(basename(record.get("run_dir"))),
            "has_target": bool(record.get("run_dir")),
            "changed_files": [
                self.text(value) for value in array(record.get("changed_files"))[:50]
                if isinstance(value, str)
            ],
        }

    def children(self, path):
        try:
            return self.files.names(path)
        except FileNotFoundError:
            return []

    def run_stamp(self, path):
        signatures = []
        for candidate, folder in ((path, True), (path / "run.json", False),
                                  (path / "steps", True), (path / "run_summary.json", False)):
            try:
                signatures.append(self.files.stamp(candidate, folder=folder))
            except FileNotFoundError:
                signatures.append(None)
        return tuple(signatures)

    def stems(self, run, files):
        engines = run.get("engines")
        aliases = set(self.models)
        aliases.update(value for value in [*array(engines), *mapping(engines).values()] if identifier(value))
        if identifier(engines):
            aliases.add(engines)
        result = {}
        for name in files:
            match = STEM.fullmatch(name)
            if not match or self.text(name, 512) != name:
                continue
            index, tail, suffix = match.groups()
            stem = f"{index}-{tail}"
            step = result.setdefault(stem, {"id": stem, "index": int(index), "files": {}})
            step["files"][suffix] = name
            if "role" in step:
                continue
            candidates = [engine for engine in aliases if tail.endswith("-" + engine)]
            if candidates:
                engine = max(candidates, key=len)
                role = tail[:-len(engine) - 1]
            elif tail.startswith("broker-worker-"):
                role, engine = "broker-worker", tail[len("broker-worker-"):]
            else:
                role, _, engine = tail.partition("-")
            step.update(role=self.text(role) or None, engine=self.text(engine) or None,
                        model=self.text(self.models.get(engine)), model_source="configured" if engine in self.models else None,
                        status=None, **usage({}))
        return sorted(result.values(), key=lambda step: (step["index"], step["id"]))

    def read_run(self, native, signature):
        path = self.settings.runs / native
        raw = self.files.json(path / "run.json")
        if raw.get("run_id") not in (None, "", native):
            raise ValueError("run ID disagrees with directory")
        kind = raw.get("kind")
        if kind not in ("task", "dispatch", "broker"):
            return None
        wrapper = kind == "broker" or raw.get("task") == "broker-request"
        files = self.children(path) if wrapper else []
        steps = self.stems(raw, self.children(path / "steps"))
        run = self.select(raw, "task", "outcome", "approval_id", "checkpoint", "checkpoint_id",
                          "gate", "refusal_reason", "tamper_reason", "scope", "scope_violation",
                          "git_metadata_tampered", "started", "finished", "sandbox_warning", "isolation_refusal_reason",
                          "brief_sha256", "pid")
        run.update(native=native, kind="broker" if wrapper else kind, steps=steps,
                   activity=max((item[3] / 1e9 for item in signature if item), default=0),
                   brief_lint=self.findings(raw.get("brief_lint")), started=run.get("started"),
                   finished=run.get("finished"))
        run["wrapper_requests"] = [name[:-13] for name in files if name.endswith(".request.json")
                                   and self.safe_id(name[:-13])]
        run["approval_links"] = [
            (self.safe_id(item.get("approval_id")), self.text(item.get("checkpoint")))
            for item in array(raw.get("checkpoints"))[:100] if isinstance(item, dict)
        ]
        if self.safe_id(raw.get("approval_id")):
            run["approval_links"].insert(0, (raw["approval_id"], run.get("checkpoint_id") or run.get("checkpoint")))
        summary = self.optional(path / "run_summary.json")
        run["summary"] = {
            **self.select(summary, "status", "verdict", "escalation_reason", "harness_error_reason", "duration_s"),
            **usage(summary),
        }
        proposals = mapping(raw.get("memory_proposals") or summary.get("memory_proposals"))
        run["memory_ids"] = [
            value for item in array(proposals.get("pending"))[:100]
            if (value := self.safe_id(item if isinstance(item, str) else mapping(item).get("id")))
        ]
        run["responses"] = {}
        for request in run["wrapper_requests"]:
            response = self.optional(path / f"{request}.response.json") or (
                mapping(raw.get("response")) if len(run["wrapper_requests"]) == 1 else {}
            )
            if response:
                run["responses"][request] = self.response(response)
            request_record = self.optional(path / f"{request}.request.json")
            reference = request_record.get("brief")
            if isinstance(reference, str) and self.text(reference, 4096) == reference:
                relative = Path(reference)
                if not relative.is_absolute() and ".." not in relative.parts:
                    run["brief_reference"] = reference
        lint = self.optional(path / ("00-brief-lint.json" if wrapper else "steps/00-brief-lint.json"))
        if lint:
            run["brief_lint"] = self.findings(lint.get("brief_lint"))
            run["brief_lint_error_count"] = number(lint.get("error_count"))
        for index, step in enumerate(steps):
            try:
                steps[index] = self.enrich_step(path, step)
            except READ_ERRORS:
                step["notice"] = "Step metadata unavailable"
        return run

    def refresh_run(self, native, *, force=False):
        path = self.settings.runs / native
        previous = self.runs.get(native)
        try:
            signature = self.run_stamp(path)
            if previous and previous[0] == signature and previous[2] == "current" and not force:
                return
            self.runs[native] = (signature, self.read_run(native, signature), "current", self.clock())
            self.deferred.discard(native)
        except DeferredRead:
            self.deferred.add(native)
        except READ_ERRORS:
            self.deferred.discard(native)
            self.runs[native] = (
                None, previous[1] if previous else None, "stale" if previous and previous[1] else "unreadable",
                previous[3] if previous else None,
            )

    def discover(self, *, full, force, selected, queue):
        now = self.monotonic()
        health = self.run_health
        discovery = full or force or now >= self.discovery_at
        if discovery or self.root_stamp is None:
            self.discovery_at = now + self.settings.discovery_poll_s
            try:
                signature = self.files.stamp(self.settings.runs, folder=True)
                health = "current"
                if signature != self.root_stamp or full or force:
                    self.ids = [name for name in self.files.names(self.settings.runs, folders=True) if self.safe_id(name)]
                    self.root_stamp = signature
                    known = set(self.ids)
                    self.runs = {key: value for key, value in self.runs.items() if key in known}
                    self.deferred.intersection_update(known)
                    self.activity = {key: value for key, value in self.activity.items() if key in known}
            except FileNotFoundError:
                self.root_stamp = None
                health = "removed" if self.ids else "missing"
            except (OSError, ValueError):
                health = "inaccessible"
        if full:
            candidates = list(reversed(self.ids))
        else:
            candidates = []
            if selected:
                candidates.append(selected)
                for native, (_, run, _, _) in self.runs.items():
                    if run and selected in run["wrapper_requests"]:
                        candidates.append(native)
                        response = run.get("responses", {}).get(selected, {})
                        candidates.append(response.get("target"))
                candidates.append(queue.get(selected, {}).get("response", {}).get("target"))
            if now >= self.active_at or force:
                self.active_at = now + self.settings.active_poll_s
                candidates.extend(key for key, (_, run, _, _) in self.runs.items()
                                  if run and run.get("outcome") in ("running", "awaiting_approval"))
            if discovery:
                # Bounded round-robin rechecks catch resumes that only modify descendants.
                batch = min(len(self.ids), 32)
                candidates.extend(
                    self.ids[-1 - (self.history_cursor + index) % len(self.ids)] for index in range(batch)
                )
                self.history_cursor += batch
            seen = self.runs.keys() | self.deferred
            initial = sorted(set(self.ids) - seen, reverse=True)[:32]
            candidates.extend(sorted(self.deferred, reverse=True)[:32])
            candidates.extend(initial)
        known = set(self.ids)
        for native in dict.fromkeys(candidates):
            if native not in known:
                continue
            self.refresh_run(native)
            if self.files.remaining == 0:
                break
        self.run_health = health
        return health

    def retained_file(self, cache, path, transform):
        signature = self.files.stamp(path)
        previous = cache.get(path)
        if previous and previous[0] == signature:
            return previous[1]
        value = transform(path)
        cache[path] = signature, value
        return value

    def queue(self, force):
        now = self.monotonic()
        if not force and now < self.queue_at:
            return self.queue_cache, self.queue_health
        self.queue_at = now + self.settings.discovery_poll_s
        queue = self.settings.workdir / ".devpack" / "queue"
        result = {}
        health = "current"
        try:
            for name in self.files.names(queue):
                match = QUEUE.fullmatch(name)
                if not match or not self.safe_id(match[1]):
                    continue
                entry = result.setdefault(match[1], {})
                if match[2] == "response":
                    entry["response"] = self.retained_file(
                        self.queue_files, queue / name, lambda path: self.response(self.files.json(path))
                    )
        except DeferredRead:
            return self.queue_cache or result, "partial"
        except FileNotFoundError:
            health = "removed" if self.queue_cache else "missing"
        except READ_ERRORS:
            health = "stale" if self.queue_cache else "unreadable"
            return self.queue_cache or result, health
        self.queue_files = {path: entry for path, entry in self.queue_files.items()
                            if (match := QUEUE.fullmatch(path.name)) and match[1] in result}
        self.queue_cache, self.queue_health = result, health
        return result, health

    def commands(self, aid):
        paths = [APPROVE_CLI, self.settings.workdir, self.settings.project_config, self.settings.state]
        args = []
        for path in paths:
            if path is None:
                args.append(None)
                continue
            raw = str(path)
            if Controls().strip(raw) != raw or any(char in raw for char in "\n\t"):
                return None
            # A shell expansion preserves the exact argv while keeping the home prefix private.
            home = Path.home()
            if path.is_relative_to(home):
                suffix = "/" + str(path.relative_to(home)) if path != home else ""
                if self.text(suffix) != suffix:
                    return None
                args.append('"$HOME"' + shlex.quote(suffix) if suffix else '"$HOME"')
            elif self.text(raw) == raw:
                args.append(shlex.quote(raw))
            else:
                return None
        cli, workdir, config, state = args
        command = f"python3 {cli} --workdir {workdir}"
        if config is not None:
            command += f" --project-config {config}"
        command += f" --state-dir {state}"
        return {
            "approve": f"{command} approve {shlex.quote(aid)}",
            "reject": f"{command} reject {shlex.quote(aid)} --note 'Reason for rejection'",
        }

    def approval(self, source, aid, folder):
        raw = self.files.json(source)
        status = raw.get("status")
        status = status.strip().lower() if isinstance(status, str) else None
        if raw.get("id") != aid or status not in (("pending",) if folder == "pending" else ("approved", "rejected")):
            raise ValueError("invalid approval record")
        checkpoint = raw.get("checkpoint")
        if not isinstance(checkpoint, str) or checkpoint not in CHECKPOINT_SPECS:
            raise ValueError("unknown checkpoint")
        record = self.select(raw, "checkpoint", "kind", "hard_stop", "subject", "created",
                             "decided_at", "decided_by", "summary", "note", "requested_by", "consumed")
        metadata = mapping(raw.get("metadata"))
        record.update(
            id=aid, status=status, target=self.safe_id(basename(raw.get("run_dir"))),
            target_recorded=bool(raw.get("run_dir")), gate_detail=self.detail_value(raw.get("gate_detail")),
            gate_receipts=self.findings(raw.get("gate_receipts")),
            memory_id=self.safe_id(metadata.get("memory_id") or raw.get("subject")),
            memory_scope=self.text(metadata.get("scope")),
            evidence_paths=[self.text(path, 4096) for path in array(raw.get("evidence"))[:20]
                            if isinstance(path, str) and self.text(path, 4096) == path],
            commands=self.commands(aid) if status == "pending" else None,
            continuation_kind=None, blocking=checkpoint in BLOCKING_CHECKPOINTS,
        )
        return record

    def continuation(self, record):
        if record.get("status") != "approved" or record.get("consumed"):
            return record
        state = self.settings.state / "runs"
        aid = record["id"]
        try:
            self.files.stamp(state / f"{aid}.consumed")
        except FileNotFoundError:
            def check(path):
                saved = self.files.json(path)
                if saved.get("consumed"):
                    return True, None, None
                if (saved.get("approval_id") == aid and saved.get("checkpoint_id") == record.get("checkpoint")
                        and saved.get("kind") in ("run", "dispatch", "broker")):
                    return False, saved["kind"], self.safe_id(basename(saved.get("run_dir")))
                return False, None, None

            consumed, workflow, target = self.retained_file(self.continuation_files, state / f"{aid}.json", check)
            if workflow:
                return {**record, "continuation_kind": workflow, "continuation_target": target}
            if consumed:
                return {**record, "consumed": True}
            return record
        return {**record, "consumed": True}

    def detail_value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return self.select(value, "status", "gate_passed", "summary", "stdout", "stderr", "reason", "error")
        return None

    def approvals(self, force):
        now = self.monotonic()
        if not force and now < self.approvals_at:
            return self.approval_cache, self.approval_health
        self.approvals_at = now + self.settings.stream_poll_s
        result = {}
        issues = False
        available = False
        try:
            for folder in ("pending", "decided"):
                base = self.settings.state / "approvals" / folder
                try:
                    files = self.files.names(base)
                except FileNotFoundError:
                    continue
                available = True
                for name in files:
                    aid = name[:-5]
                    if not name.endswith(".json") or not self.safe_id(aid):
                        continue
                    try:
                        record = self.retained_file(
                            self.approval_files, base / name, lambda path: self.approval(path, aid, folder)
                        )
                        if record.get("blocking"):
                            try:
                                record = self.continuation(record)
                            except FileNotFoundError:
                                pass
                            except READ_ERRORS:
                                record = {**record, "continuation_notice": "Continuation unavailable"}
                                issues = True
                        result[aid] = record
                    except READ_ERRORS:
                        if aid in self.approval_cache and aid not in result:
                            result[aid] = {**self.approval_cache[aid], "commands": None}
                        issues = True
        except DeferredRead:
            return self.approval_cache or result, "partial"
        except READ_ERRORS:
            return {aid: {**record, "commands": None} for aid, record in self.approval_cache.items()}, "inaccessible"
        valid_approval_paths = {
            self.settings.state / "approvals" / folder / f"{aid}.json"
            for aid in result for folder in ("pending", "decided")
        }
        self.approval_files = {
            path: entry for path, entry in self.approval_files.items() if path in valid_approval_paths
        }
        self.continuation_files = {
            path: entry for path, entry in self.continuation_files.items() if path.stem in result
        }
        if not available:
            try:
                self.files.stamp(self.settings.state, folder=True)
                available = True
            except READ_ERRORS:
                pass
        self.approval_health = "stale" if issues else "current" if available else "missing"
        self.approval_cache = result
        return result, self.approval_health

    def join_approvals(self, runs, approvals):
        by_id = defaultdict(list)
        linked = defaultdict(list)
        for native, run in runs.items():
            for aid, checkpoint in run["approval_links"]:
                if aid and aid in approvals and (not checkpoint or approvals[aid].get("checkpoint") == checkpoint):
                    by_id[aid].append(native)
        result = []
        for aid, record in approvals.items():
            candidates = set(by_id[aid])
            if not candidates:
                if record.get("checkpoint") == "memory-decision":
                    proposer = record.get("requested_by") or ""
                    explicit = proposer[4:] if proposer.startswith("run:") else None
                    matches_id = {
                        native for native, run in runs.items() if record["memory_id"] in run["memory_ids"]
                    }
                    if explicit:
                        if explicit in runs and (not runs[explicit]["memory_ids"] or explicit in matches_id):
                            candidates.add(explicit)
                    else:
                        candidates = matches_id
                elif record["target"] in runs:
                    candidates.add(record["target"])
            native = next(iter(candidates)) if len(candidates) == 1 else None
            observation = {**record, "run_id": native, "link": "linked" if native else "unresolved"}
            if native:
                linked[native].append(observation)
            result.append(observation)
        return linked, result

    def enrich_step(self, path, step):
        step = {**step}
        if filename := step["files"].get("meta.json"):
            meta = self.files.json(path / "steps" / filename)
            step.update(self.select(meta, "role", "engine", "status", "exit_code", "error", "stderr",
                                    "final_text", "duration_s", "memory_tokens"))
            step["memory_ids"] = [value for item in array(meta.get("memory_ids"))[:100]
                                  if (value := self.safe_id(item))]
            step["memory_hashes"] = {
                key: value for item, digest in list(mapping(meta.get("memory_hashes")).items())[:100]
                if (key := self.safe_id(item)) and (value := self.safe_id(digest))
            }
            step.update(usage(meta))
            if model := self.text(meta.get("init_model") or meta.get("model")):
                step.update(model=model, model_source="observed" if meta.get("init_model") else "reported")
            if "duration_s" not in step and number(meta.get("duration_ms")) is not None:
                step["duration_s"] = meta["duration_ms"] / 1000
        return step

    def gate_prefix(self, path):
        """Recover complete top-level verdict fields before an oversized value."""
        data = self.files.read(path, JSON_LIMIT, prefix=True).decode("utf-8", errors="replace").lstrip()
        result = {}
        if not data.startswith("{"):
            return result
        decoder = json.JSONDecoder()
        whitespace = re.compile(r"\s*")
        cursor = 1
        while cursor < len(data):
            try:
                key, end = decoder.raw_decode(data, whitespace.match(data, cursor).end())
                colon = whitespace.match(data, end).end()
                if not isinstance(key, str) or data[colon:colon + 1] != ":":
                    break
                value, end = decoder.raw_decode(data, whitespace.match(data, colon + 1).end())
            except (ValueError, RecursionError):
                break
            separator = whitespace.match(data, end).end()
            if data[separator:separator + 1] not in (",", "}"):
                break
            if key in ("gate_passed", "gate_status", "returncode", "step_index", "round",
                       "duration_s", "lint_clean", "static_clean") and type(value) in (bool, int, float, str):
                result[key] = value
            if data[separator] == "}":
                break
            cursor = separator + 1
        return result

    def gate_at(self, path, step, receipt=False):
        oversized = False
        try:
            raw = self.files.json(path)
        except OversizedArtifact:
            raw = self.gate_prefix(path)
            if not raw:
                raise
            oversized = True
        flags = self.select(raw, "gate_status", "step_index", "round", "returncode", "command",
                            "duration_s", "timestamp", "timestamp_utc", "lint_clean", "static_clean",
                            "stdout", "stderr", "first_failed_check", "error")
        if oversized:
            flags["artifact_notice"] = "Gate artifact exceeds read limit. Showing available verdict fields."
        if isinstance(raw.get("command"), list):
            flags["command"] = self.text(shlex.join([part for part in raw["command"] if isinstance(part, str)]))
        verdict = raw.get("gate_passed")
        status = flags.get("gate_status") if receipt else None
        return {
            **flags, "step": step, "status": ("pass" if verdict else "fail") if type(verdict) is bool else status,
            "lint_findings": self.findings(raw.get("lint_findings")),
            "static_findings": self.findings(raw.get("static_findings")),
            "checks": self.findings(raw.get("checks")),
        }

    def gates(self, native):
        root = self.settings.runs / native
        directories = [root]
        for configured in self.settings.log_roots:
            directories.append(configured / native)
            # An explicitly selected directory may be the log directory itself.
            try:
                summary = self.files.json(configured / "run_summary.json")
            except READ_ERRORS:
                continue
            if summary.get("run_id") == native or basename(summary.get("run_dir")) == native:
                directories.append(configured)
        result = {}
        notices = []
        for directory in directories:
            for name in self.children(directory):
                match = GATE.fullmatch(name)
                if match and int(match[1]) not in result:
                    step = int(match[1])
                    try:
                        result[step] = self.gate_at(directory / name, step)
                    except READ_ERRORS:
                        notices.append(f"Gate artifact unreadable for step {step}")
        for name in self.children(root / "steps"):
            match = GATE_TEXT.fullmatch(name)
            if match:
                step = int(match[1])
                gate = result.setdefault(step, {"step": step, "status": None})
                gate["transcript"] = self.excerpt(root / "steps" / name)
        receipt = root / "steps" / "02-gate-receipt.json"
        if receipt.name in self.children(receipt.parent):
            try:
                result[2] = {**result.get(2, {}), **self.gate_at(receipt, 2, receipt=True)}
            except READ_ERRORS:
                notices.append("Gate receipt unreadable for step 2")
        return [result[step] for step in sorted(result)], notices

    def make_job(self, native, run, approvals, request=None, wrapper=None, response=None, notices=()):
        source = self.settings.runs
        run = run or {}
        outcome = run.get("outcome")
        summary = run.get("summary", {})
        if not outcome:
            outcome = summary.get("verdict") or summary.get("status")
        state = OUTCOMES.get(outcome)
        if run.get("scope_violation") or run.get("git_metadata_tampered") or run.get("scope") == "violation":
            state = "failed"
        if run.get("kind") == "broker" and state == "failed" and response and response.get("status") == "refused":
            state = "refused"
        related = approvals.get(native, [])
        pause = next((approval for approval in related if approval["id"] == run.get("approval_id")), None)
        messages = list(notices)
        escalation_pause = (
            outcome == "ESCALATE_TO_HUMAN" and pause and pause["blocking"]
            and not pause.get("consumed") and pause["checkpoint"] != "brief"
            and pause["checkpoint"] == (run.get("checkpoint_id") or run.get("checkpoint"))
            and all(checkpoint not in TERMINAL_CHECKPOINTS for _, checkpoint in run["approval_links"])
        )
        if state == "waiting for approval" or escalation_pause:
            state = "waiting for approval"
            if pause:
                if pause.get("status") == "rejected":
                    state = "rejected"
                elif pause.get("consumed"):
                    messages.append("Resume status unknown")
                elif (
                    pause.get("continuation_kind")
                    and pause["continuation_kind"] == {"task": "run"}.get(run.get("kind"), run.get("kind"))
                    and pause.get("continuation_target") in (None, native)
                ):
                    state = "ready to resume"
            else:
                messages.append("Approval link unknown")
        steps = run.get("steps", [])
        latest = steps[-1] if steps else {}
        started = timestamp(run.get("started"))
        finished = timestamp(run.get("finished"))
        age = max(0, self.clock() - started) if started is not None else None
        duration = finished - started if state in TERMINAL and started is not None and finished is not None and finished >= started else None
        aliases = list(dict.fromkeys(item for item in (native, (wrapper or {}).get("native")) if item))
        observation = None
        status = "current"
        if native in self.runs:
            _, _, status, observation = self.runs[native]
        if status != "current":
            messages.append("Run metadata is stale")
        if not steps and state == "running":
            messages.append("No step output observed")
        if state is None and run:
            messages.append("Execution outcome unknown")
        if request and native:
            worker = native if wrapper and native != wrapper["native"] else None
        else:
            worker = None
        last_event = self.activity.get(native)
        event_age = max(0, self.clock() - last_event) if last_event is not None else None
        quiet = state == "running" and event_age is not None and event_age >= self.settings.quiet_threshold_s
        job = {
            "backend": "pack", "source": self.text(str(source), 4096), "id": request or native,
            "key": ["pack", self.text(str(source), 4096), "request" if request else "run", request or native],
            "aliases": aliases, "native_id": native, "kind": "broker" if request else run.get("kind"),
            "state": state, "native_outcome": run.get("outcome"), "summary_status": summary.get("status"),
            "summary_verdict": summary.get("verdict"), "broker_disposition": (wrapper or {}).get("outcome"),
            "broker_membership": "linked" if worker else None, "worker_id": worker,
            "broker_response": response, "task": run.get("task"), "role": latest.get("role"),
            "agent": latest.get("engine"), "engine": latest.get("engine"), "model": latest.get("model"),
            "model_source": latest.get("model_source"), "current_step": latest.get("index"),
            "started": run.get("started"), "finished": run.get("finished"), "age_s": age,
            "duration_s": duration, "invocation_duration_s": summary.get("duration_s"),
            "process_health": None, "last_event_time": last_event, "last_event_age_s": event_age,
            "activity_time": run.get("activity"), "activity": "quiet" if quiet else None, "health": status,
            "last_good_at": observation, "read_failed": False, "gate": run.get("gate"), "approval_ids": [record["id"] for record in related],
            "steps": [{key: value for key, value in step.items() if key != "files"} for step in steps],
            "gates": [], "notices": messages, "diagnostics": {
                name: run.get(name) for name in ("refusal_reason", "tamper_reason", "scope_violation",
                                                 "git_metadata_tampered", "sandbox_warning", "isolation_refusal_reason") if run.get(name)
            },
            "evidence": {"brief_lint": run.get("brief_lint", []),
                         "escalation_reason": summary.get("escalation_reason")},
        }
        if wrapper and wrapper["native"] != native:
            job["approval_ids"].extend(record["id"] for record in approvals.get(wrapper["native"], []))
        if response and response.get("error"):
            job["diagnostics"]["broker_error"] = response["error"]
        if summary.get("harness_error_reason"):
            job["diagnostics"]["harness_error_reason"] = summary["harness_error_reason"]
        for field in ("cost_usd", "tokens_in", "tokens_out"):
            known = [step[field] for step in steps if step[field] is not None]
            summary_value = summary.get(field) if run.get("outcome") in (None, summary.get("status"), summary.get("verdict")) else None
            job[field] = summary_value if summary_value is not None else number(sum(known)) if known else None
            job[field + "_coverage"] = (
                {"source": "summary", "known": None, "eligible": None} if summary_value is not None else
                {"source": "steps", "known": len(known), "eligible": len(steps)}
            )
        return job

    def broker_jobs(self, runs, queue, linked):
        wrappers = defaultdict(list)
        for native, run in runs.items():
            for request in run["wrapper_requests"]:
                wrappers[request].append(native)
        connections = {}
        target_counts = defaultdict(int)
        for request in sorted(set(queue) | set(wrappers)):
            entries = wrappers[request]
            wrapper = runs[entries[0]] if len(entries) == 1 else None
            responses = [record for record in (
                (wrapper or {}).get("responses", {}).get(request), queue.get(request, {}).get("response")
            ) if record]
            targets = {record["target"] for record in responses if record["has_target"]}
            worker = None
            notice = None
            if len(entries) > 1 or len(targets) > 1:
                notice = "Conflicting broker links"
            elif wrapper and targets == {wrapper["native"]}:
                pass
            elif wrapper and len(targets) == 1:
                target = next(iter(targets))
                if target and target in runs and runs[target]["kind"] == "dispatch":
                    worker = target
                    target_counts[target] += 1
                else:
                    notice = "Worker link unknown"
            else:
                notice = "Worker link unknown" if wrapper else "Processing status unconfirmed"
            connections[request] = (wrapper, worker, responses[-1] if responses else None, notice)
        jobs = []
        collapsed = set()
        for request, (wrapper, worker, response, notice) in connections.items():
            if worker and target_counts[worker] > 1:
                worker = None
                notice = "Conflicting broker links"
            native = worker or (wrapper or {}).get("native")
            collapsed.update(item for item in (native, (wrapper or {}).get("native")) if item)
            jobs.append(self.make_job(native, runs.get(native), linked, request, wrapper, response,
                                      [notice] if notice else []))
        jobs.extend(self.make_job(native, run, linked) for native, run in runs.items() if native not in collapsed)
        return jobs

    def evidence(self, approval, run):
        result = {
            "brief": None, "brief_status": "unavailable", "brief_lint": (run or {}).get("brief_lint", []),
            "brief_lint_error_count": (run or {}).get("brief_lint_error_count"),
            "escalation_reason": (run or {}).get("summary", {}).get("escalation_reason"),
            "excerpts": [],
        }
        if run and approval.get("checkpoint") == "brief":
            native = run["native"]
            path = self.settings.runs / native
            prompts = [step["files"]["prompt.md"] for step in run["steps"]
                       if step["index"] == 1 and "prompt.md" in step["files"]]
            if len(prompts) == 1:
                result["brief"] = self.excerpt(path / "steps" / prompts[0])
            if result["brief"] is None and run.get("brief_reference") and run.get("brief_sha256"):
                try:
                    data = self.files.read(self.settings.workdir / run["brief_reference"], JSON_LIMIT)
                    if hashlib.sha256(data).hexdigest() == run["brief_sha256"]:
                        result["brief"] = self.text(data.decode("utf-8", errors="replace"))
                except READ_ERRORS:
                    pass
            if result["brief"] is not None:
                result["brief_status"] = "available"
        for reference in approval["evidence_paths"]:
            path = Path(reference)
            if path.is_absolute() and ".." not in path.parts and any(
                path.is_relative_to(root) for root in self.settings.evidence_roots
            ):
                if excerpt := self.excerpt(path):
                    result["excerpts"].append({"name": self.text(path.name), "text": excerpt})
        if approval.get("checkpoint") == "memory-decision" and approval.get("memory_id") and approval.get("memory_scope"):
            try:
                folder = scope_dir_name(approval["memory_scope"])
            except ValueError:
                folder = None
            if folder:
                root = self.settings.workdir / ".devpack" / "memory"
                path = root / folder / (approval["memory_id"] + ".md")
                if excerpt := self.excerpt(path):
                    result["excerpts"].append({"name": self.text(path.name), "text": excerpt})
        return result

    def selected_events(self, job, run, collect):
        streams = [step["files"]["jsonl"] for step in (run or {}).get("steps", []) if "jsonl" in step["files"]]
        if not streams:
            return [], "missing", None
        key = (run["native"], streams[-1])
        if self.stream_key != key:
            self.stream_key = key
            self.stream = Stream(self.redactor, self.clock)
        stream = self.stream
        budget = TICK_LIMIT if self.files.remaining is None else self.files.remaining
        path = self.settings.runs / key[0] / "steps" / key[1]
        status, used = stream.poll(path, terminal=job["state"] in TERMINAL, budget=budget,
                                   collect=collect, root=self.files.root_for(path))
        if self.files.remaining is not None:
            self.files.remaining -= used
        if stream.last_event is not None:
            observed = timestamp(stream.last_event_timestamp)
            job["last_event_time"] = stream.last_event if observed is None else observed
            self.activity[run["native"]] = job["last_event_time"]
            job["last_event_age_s"] = max(0, self.clock() - job["last_event_time"])
            if job["state"] == "running" and job["last_event_age_s"] >= self.settings.quiet_threshold_s:
                job["activity"] = "quiet"
        job["stream_mtime"] = stream.mtime
        job["stream_errors"] = stream.errors
        if status == "empty":
            job["notices"].append("No stream output yet (buffered or non-streaming)")
        return stream.batch if collect else list(stream.events), status, stream

    def snapshot(self, *, query="", job_id=None, event_kind=None, approval_id=None, approval_status="all",
                 interactive=False, force=False, include_evidence=False, include_gates=True, stream_batch=False):
        self.files.remaining = TICK_LIMIT if interactive else None
        look_up = job_id.removeprefix("pack:") if job_id else None
        if look_up and not identifier(look_up):
            raise ValueError("invalid Pack job ID")
        if interactive and event_kind and self.stream_key is not None:
            # Keep capacity for stream reads even when historical discovery has a backlog.
            self.files.remaining -= TICK_LIMIT // 2
        queue, queue_health = self.queue(force or not interactive)
        approval_records, approval_health = self.approvals(force or not interactive)
        run_health = self.discover(full=not interactive, force=force, selected=look_up, queue=queue)
        if interactive:
            targets = list(dict.fromkeys(record["target"] for record in approval_records.values()
                                         if record["target"] and record["target"] not in self.runs))
            known = set(self.ids)
            for native in targets[:20]:
                if native in known:
                    self.refresh_run(native)
        runs = {native: run for native, (_, run, _, _) in self.runs.items() if run}
        linked, approvals = self.join_approvals(runs, approval_records)
        jobs = self.broker_jobs(runs, queue, linked)
        active_jobs = any(run.get("outcome") == "running" for run in runs.values())
        jobs.sort(key=lambda job: (job["activity_time"] or 0, job["id"]), reverse=True)
        ambiguous = False
        if look_up:
            direct = [job for job in jobs if look_up == job["id"] or look_up in job["aliases"]]
            candidates = direct or [job for job in jobs if job["id"].endswith(look_up)]
            ambiguous = len(candidates) > 1
            if ambiguous and (not interactive or event_kind):
                ids = ", ".join(sorted(job["id"] for job in candidates))
                print(f"Ambiguous Pack job ID {look_up}: {ids}", file=sys.stderr)
            jobs = candidates if not ambiguous else []
        else:
            jobs = [job for job in jobs if matches(job, terms(query))]
        matched = len(jobs)
        jobs = jobs[:self.settings.history_limit]
        events = []
        stream_health = None
        stream_drained = False
        for job in jobs if look_up and not ambiguous else []:
            run = runs.get(job["native_id"])
            if run and include_gates:
                native = run["native"]
                try:
                    self.gate_snapshot = self.gates(native)
                    self.gate_key = native
                    gates, notices = self.gate_snapshot
                    job["read_failed"] = bool(notices and not any(
                        gate.get(field) is not None for gate in gates
                        for field in ("status", "returncode", "transcript", "stderr")
                    ))
                except DeferredRead:
                    job["notices"].append("Gate reads deferred")
                except READ_ERRORS:
                    job["notices"].append("Gate artifact unreadable")
                    job["read_failed"] = True
                if self.gate_key == native:
                    job["gates"], gate_notices = self.gate_snapshot
                    job["notices"].extend(gate_notices)
                    if not job["gate"] and run["kind"] == "task" and job["gates"]:
                        job["gate"] = job["gates"][-1]["status"]
            if event_kind:
                if interactive and self.stream_key is not None:
                    self.files.remaining += TICK_LIMIT // 2
                events, stream_health, stream = self.selected_events(job, run, stream_batch)
                stream_drained = bool(stream and stream.stable >= 2 and job["state"] in TERMINAL)
        pending_count = sum(approval.get("status") == "pending" for approval in approvals)
        approvals = [
            approval for approval in approvals if approval_status == "all"
            or (approval.get("status") == "pending") == (approval_status == "pending")
        ]
        approvals.sort(key=lambda approval: (approval.get("created") or "", approval["id"]), reverse=True)
        for approval in approvals:
            if include_evidence and (not approval_id or approval["id"] == approval_id):
                try:
                    approval["evidence"] = self.evidence(approval, runs.get(approval["run_id"]))
                except DeferredRead:
                    approval["evidence"] = None
            for key in ("target", "target_recorded", "evidence_paths", "memory_scope", "continuation_kind", "continuation_target"):
                approval.pop(key, None)
        unreadable = sum(health != "current" for _, _, health, _ in self.runs.values())
        now = self.clock()
        return {
            "schema_version": 1, "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "observed_epoch": now, "sources": {"runs": run_health, "queue": queue_health,
                                               "approvals": approval_health, "stream": stream_health},
            "scan": {"complete": run_health == "current" and len(self.ids) == len(self.runs)
                                  and not self.deferred and not unreadable and queue_health in ("current", "missing"),
                     "discovered": len(self.ids), "scanned": len(self.runs), "matched": matched,
                     "returned": len(jobs), "unreadable": unreadable, "history_limit": self.settings.history_limit},
            "pending_approvals": pending_count, "jobs": jobs, "approvals": approvals,
            "events": [event for event in events if event_kind == "all" or event["kind"] in (event_kind, "notice")],
            "stream_drained": stream_drained, "ambiguous": ambiguous,
            **({"active_jobs": active_jobs} if interactive else {}),
        }
