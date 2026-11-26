"""Incremental, bounded parsing of the selected Pack stream."""

from collections import deque
import json
import os

from .files import Controls, RECORD_LIMIT, TICK_LIMIT, array, clean, mapping, regular

KNOWN = {
    "system", "assistant", "user", "result", "message", "tool_use", "tool_result",
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed",
}


class Stream:
    def __init__(self, redactor, clock):
        self.redactor = redactor
        self.clock = clock
        self.events = deque(maxlen=1000)
        self.batch = None
        self.identity = None
        self.sequence = 0
        self.last_event = None
        self.last_event_timestamp = None
        self.mtime = None
        self.reset()

    def reset(self):
        self.offset = 0
        self.prefix = b""
        self.suffix = b""
        self.partial = b""
        self.discard = False
        self.format = "empty"
        self.errors = 0
        self.stable = 0
        self.controls = Controls()
        self.calls = {}

    def emit(self, kind, text, **fields):
        self.sequence += 1
        observed = self.clock()
        event = {"sequence": self.sequence, "kind": kind, "text": text, "observed_at": observed, **fields}
        if kind != "notice":
            self.last_event = observed
            self.last_event_timestamp = fields.get("timestamp")
        self.events.append(event)
        if self.batch is not None:
            self.batch.append(event)

    def excerpt(self, value):
        if value is None:
            return None
        if not isinstance(value, str):
            def strip_values(item):
                if isinstance(item, dict):
                    return {Controls().strip(key): strip_values(val) for key, val in item.items()}
                if isinstance(item, list):
                    return [strip_values(val) for val in item]
                return Controls().strip(item) if isinstance(item, str) else item

            value = json.dumps(strip_values(value), ensure_ascii=False)
        return clean(self.redactor, value)

    def block(self, block, timestamp):
        kind = block.get("type")
        if kind in ("tool_use", "tool_result", "command_execution", "mcp_tool_call"):
            call_id = clean(self.redactor, block.get("tool_use_id", block.get("id")), 128)
            name = clean(self.redactor, block.get("name", block.get("tool")))
            phase = "result" if kind == "tool_result" or block.get("phase") == "result" else "call"
            if call_id and name:
                if len(self.calls) >= 1000:
                    self.calls.pop(next(iter(self.calls)))
                self.calls[call_id] = name
            text = block.get("input", block.get("command", block.get("content", block.get("output"))))
            if phase == "result":
                text = block.get("aggregated_output", block.get("output", block.get("content", text)))
            self.emit(
                "tool", self.excerpt(text), timestamp=timestamp, phase=phase, call_id=call_id,
                tool=name or self.calls.get(call_id), is_error=block.get("is_error") is True,
            )
        elif kind in ("text", "agent_message", "message"):
            self.emit("message", self.excerpt(block.get("text", block.get("content"))), timestamp=timestamp)

    def record(self, raw):
        text = self.controls.strip(raw.decode("utf-8", errors="replace")).strip()
        if not text:
            return
        if self.format != "plain":
            try:
                event = mapping(json.loads(text))
            except (ValueError, RecursionError):
                event = {}
            kind = event.get("type")
            if not isinstance(kind, str) or kind not in KNOWN:
                if self.format == "jsonl":
                    self.errors += 1
                    self.emit("notice", "Unrecognized or malformed JSONL record")
                    return
                if text.startswith("{") or event:
                    self.format = "uncertain"
                    self.emit("notice", "Uncertain stream format")
                    return
                if self.format == "empty":
                    self.format = "plain"
            else:
                self.format = "jsonl"
                timestamp = clean(self.redactor, event.get("timestamp", event.get("timestamp_utc")), 64)
                if kind in ("assistant", "user", "message"):
                    content = mapping(event.get("message")).get("content", event.get("content"))
                    if isinstance(content, str):
                        self.emit("message", self.excerpt(content), timestamp=timestamp)
                    for block in array(content):
                        self.block(mapping(block), timestamp)
                elif kind.startswith("item."):
                    item = dict(mapping(event.get("item")))
                    if kind != "item.updated":
                        item["phase"] = "call" if kind == "item.started" else "result"
                        self.block(item, timestamp)
                elif kind.startswith("tool_"):
                    self.block(event, timestamp)
                else:
                    self.emit(
                        "message" if kind == "result" else "activity",
                        self.excerpt(event.get("result")) or kind, timestamp=timestamp,
                    )
                return
        self.emit("plain", self.excerpt(text))

    def feed(self, data):
        parts = data.split(b"\n")
        for part in parts[:-1]:
            if not self.discard and len(self.partial) + len(part) <= RECORD_LIMIT:
                self.record(self.partial + part)
            else:
                if self.format == "jsonl":
                    self.errors += 1
                self.emit("notice", "Oversized stream record")
                self.controls = Controls()
            self.partial = b""
            self.discard = False
        last = parts[-1]
        if self.discard or len(self.partial) + len(last) > RECORD_LIMIT:
            self.partial = b""
            self.discard = True
        else:
            self.partial += last

    def poll(self, path, *, terminal=False, budget=TICK_LIMIT, collect=False, root=None):
        self.batch = [] if collect else None
        used = 0
        try:
            with regular(path, root) as (fd, info):
                identity = (info.st_dev, info.st_ino)
                prefix = os.pread(fd, min(len(self.prefix), budget), 0)
                used += len(prefix)
                suffix_count = min(len(self.suffix), budget - used)
                suffix = os.pread(fd, suffix_count, self.offset - len(self.suffix))
                used += len(suffix)
                if self.identity is not None and (
                    identity != self.identity or info.st_size < self.offset
                    or prefix != self.prefix[:min(len(self.prefix), budget)]
                    or suffix != self.suffix[:suffix_count]
                ):
                    self.reset()
                    self.emit("notice", "Stream gap (replaced or truncated)")
                self.identity = identity
                self.mtime = info.st_mtime
                data = os.pread(fd, max(0, budget - used), self.offset)
                if not self.offset:
                    self.prefix = data[:64]
                elif len(self.prefix) < 64 and self.offset == len(self.prefix):
                    self.prefix += data[:64 - len(self.prefix)]
                self.offset += len(data)
                self.suffix = (self.suffix + data)[-64:]
                used += len(data)
                self.feed(data)
                if data or self.offset < info.st_size:
                    self.stable = 0
                else:
                    self.stable += 1
                if terminal and self.stable == 2 and self.partial and not self.discard:
                    if self.format != "plain":
                        try:
                            valid = mapping(json.loads(self.partial)).get("type")
                        except (ValueError, RecursionError):
                            valid = None
                        if isinstance(valid, str) and valid in KNOWN:
                            self.record(self.partial)
                            self.partial = b""
                status = self.format
                if info.st_size and status == "empty":
                    status = "uncertain"
                return status, used
        except FileNotFoundError:
            return ("removed" if self.identity else "missing"), used
        except (OSError, ValueError):
            return "inaccessible", used
