"""Write and verify heartbeat records for locally owned runs."""

from contextvars import ContextVar
from functools import lru_cache, wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import uuid

INTERVAL = 5
SCHEMA_VERSION = 1
_scopes = ContextVar("heartbeat_scopes", default=())


@lru_cache(maxsize=1)
def host_id():
    try:
        machine = Path("/etc/machine-id").read_text(encoding="ascii").strip() if sys.platform == "linux" else ""
    except (OSError, ValueError):
        machine = ""
    if not machine:
        machine = f"{uuid.getnode():012x}"
        if int(machine[:2], 16) & 1:  # Random fallback, not a verifiable host identity.
            return None
    return hashlib.sha256(f"{socket.gethostname()}:{machine}".encode()).hexdigest()


@lru_cache(maxsize=1)
def boot_id():
    if sys.platform == "linux":
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
        except (OSError, ValueError):
            pass
    return None


def pid_identity(pid):
    """Return process start identity and PID namespace, if supported."""
    if sys.platform == "linux":
        proc = Path("/proc") / str(pid)
        try:
            fields = (proc / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                raise ProcessLookupError(pid)
            namespace = os.readlink(proc / "ns/pid")
            if not boot_id():
                return None, namespace
            return fields[19], namespace
        except FileNotFoundError as exc:
            # Distinguish an absent PID from a missing or restricted procfs.
            os.kill(pid, 0)
            raise exc
        except IndexError as exc:
            raise ValueError("invalid process stat") from exc
    if sys.platform == "darwin":
        import ctypes

        class ProcBSDInfo(ctypes.Structure):
            # Fixed prefix followed by two uint64 start time fields in proc_bsdinfo.
            _fields_ = [("prefix", ctypes.c_uint32 * 30), ("seconds", ctypes.c_uint64),
                        ("microseconds", ctypes.c_uint64)]

        info = ProcBSDInfo()
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        size = ctypes.sizeof(info)
        if libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(info), size) != size:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if info.prefix[1] == 5:  # SZOMB
            raise ProcessLookupError(pid)
        return f"{info.seconds}.{info.microseconds:06d}", None
    return None, None


def process_health(record, run_id, now):
    pid = record.get("pid")
    written = record.get("heartbeat_at")
    start = record.get("pid_start_identity")
    local_host, local_boot = host_id(), boot_id()
    if (
        type(record.get("schema_version")) is not int or record["schema_version"] != SCHEMA_VERSION
        or record.get("run_id") != run_id or not local_host or record.get("host_id") != local_host
        or record.get("boot_id") != local_boot or not isinstance(start, str) or not start
        or type(pid) is not int or not 0 < pid <= 2**31 - 1
        or type(written) not in (int, float) or not 0 <= written <= now
    ):
        return "unknown"
    namespace = record.get("pid_namespace")
    if local_boot and not namespace:
        return "unknown"
    stale = now - written >= 3 * INTERVAL
    try:
        actual = pid_identity(pid)
    except ProcessLookupError:
        return "stale" if stale else "unknown"
    except (OSError, ValueError):
        return "unknown"
    if actual != (start, namespace):
        return "unknown"
    return "stale" if stale else "alive"


class Heartbeat:
    def __init__(self, run_dir, run_id):
        self.path = run_dir / "heartbeat.json"
        pid = os.getpid()
        try:
            start, namespace = pid_identity(pid)
        except (OSError, ValueError):
            start, namespace = None, None
        self.identity = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id, "pid": pid,
            "host_id": host_id(), "boot_id": boot_id(),
            "pid_start_identity": start, "pid_namespace": namespace,
        }
        self.last_event_at = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.refresh, name="run-heartbeat", daemon=True)

    def event(self):
        self.last_event_at = time.time()

    def write(self):
        record = {
            **self.identity, "heartbeat_at": time.time(), "last_event_at": self.last_event_at,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix=".heartbeat-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(record, handle, allow_nan=False)
                handle.write("\n")
                handle.close()
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def wait(stop, interval):
        return stop.wait(interval)

    def try_write(self):
        try:
            self.write()
        except OSError:
            logging.getLogger(__name__).warning("Could not write run heartbeat", exc_info=True)

    def refresh(self):
        while not self.wait(self.stop, INTERVAL):
            if self.stop.is_set():
                break
            self.try_write()

    def close(self):
        self.stop.set()
        self.thread.join()


def owns_runs(func):
    """Stop any remaining heartbeat writers when an owning call exits."""
    @wraps(func)
    def call(*args, **kwargs):
        owned = {}
        token = _scopes.set((*_scopes.get(), owned))
        try:
            return func(*args, **kwargs)
        finally:
            _scopes.reset(token)
            for owner in owned.values():
                owner.close()

    return call


def current(run_dir):
    path = Path(run_dir).absolute()
    return next((scope[path] for scope in reversed(_scopes.get()) if path in scope), None)


def start(run_dir, run_id):
    path = Path(run_dir).absolute()
    scopes = _scopes.get()
    if current(path) is not None:
        return
    owner = Heartbeat(path, run_id)
    owner.try_write()
    if scopes:
        owner.thread.start()
        scopes[-1][path] = owner


def finish(run_dir):
    path = Path(run_dir).absolute()
    for scope in reversed(_scopes.get()):
        owner = scope.pop(path, None)
        if owner is not None:
            owner.close()
            if owner.last_event_at is not None:
                owner.try_write()
            break
