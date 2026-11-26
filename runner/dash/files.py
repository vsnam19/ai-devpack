"""Bounded artifact reads without following filesystem links."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

RECORD_LIMIT = 256 * 1024
JSON_LIMIT = 1024 * 1024 - 1
TICK_LIMIT = 2 * 1024 * 1024
EXCERPT = 1600
TEXT_LIMIT = RECORD_LIMIT
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)


def identifier(value):
    return isinstance(value, str) and ".." not in value and bool(IDENTIFIER.fullmatch(value))


def mapping(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else []


def resolve_root_parent(root):
    """Resolve trusted ancestors while leaving the root entry for no-follow validation."""
    return Path(os.path.realpath(root.parent)) / root.name


@contextmanager
def directory(path, root=None):
    """Open the configured root, then pin each descendant without following links."""
    absolute = Path(os.path.abspath(path))
    root = Path(absolute.anchor) if root is None else root
    parts = absolute.relative_to(root).parts
    resolved = resolve_root_parent(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(resolved, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular(path, root=None):
    with directory(path.parent, root) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("expected a regular file")
            yield fd, info
        finally:
            os.close(fd)


def names(path, *, folders=False, root=None):
    with directory(path, root) as fd, os.scandir(fd) as entries:
        return sorted(
            entry.name for entry in entries
            if (entry.is_dir if folders else entry.is_file)(follow_symlinks=False)
        )


def stamp(path, *, folder=False, root=None):
    if folder:
        with directory(path, root) as fd:
            info = os.fstat(fd)
    else:
        with regular(path, root) as (_, info):
            pass
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class DeferredRead(Exception):
    """The poll budget was spent before this read."""


class OversizedArtifact(ValueError):
    """An artifact exceeded its normal read limit."""


class Files:
    def __init__(self, *roots):
        self.roots = tuple(dict.fromkeys(Path(os.path.abspath(root)) for root in roots))
        self.remaining = None

    def root_for(self, path):
        absolute = Path(os.path.abspath(path))
        return next((root for root in self.roots if absolute.is_relative_to(root)), None)

    def names(self, path, *, folders=False):
        return names(path, folders=folders, root=self.root_for(path))

    def stamp(self, path, *, folder=False):
        return stamp(path, folder=folder, root=self.root_for(path))

    def read(self, path, limit, *, prefix=False):
        with regular(path, self.root_for(path)) as (fd, info):
            if info.st_size > limit and not prefix:
                raise OversizedArtifact("artifact exceeds read limit")
            size = min(info.st_size, limit)
            count = size if prefix else size + 1
            if self.remaining is not None and self.remaining < count:
                raise DeferredRead
            data = os.read(fd, count)
            if self.remaining is not None:
                self.remaining -= len(data)
            current = os.fstat(fd)
            if len(data) != size or (current.st_size, current.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise ValueError("artifact changed during read")
            return data

    def json(self, path):
        result = json.loads(self.read(path, JSON_LIMIT))
        if not isinstance(result, dict):
            raise ValueError("expected a JSON object")
        return result


class Controls:
    """Discard terminal control sequences, including strings spanning records."""

    def __init__(self):
        self.state = ""

    def strip(self, value):
        visible = []
        for char in value:
            code = ord(char)
            if self.state == "string":
                if char in ("\x07", "\x9c"):
                    self.state = ""
                elif char == "\x1b":
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "" if char == "\\" else "string"
            elif self.state == "csi":
                if 0x40 <= code <= 0x7e:
                    self.state = ""
            elif self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]PX^_":
                    self.state = "string"
                elif not 0x20 <= code <= 0x2f:
                    self.state = ""
            elif char == "\x1b":
                self.state = "escape"
            elif char == "\x9b":
                self.state = "csi"
            elif char in "\x90\x98\x9d\x9e\x9f":
                self.state = "string"
            elif char in "\n\t" or (code >= 32 and not unicodedata.category(char).startswith("C")):
                visible.append(char)
        return "".join(visible)


def clean(redactor, value, limit=EXCERPT):
    if not isinstance(value, str):
        return None
    if len(value) > TEXT_LIMIT:
        return "[Text exceeds display limit]"[:limit]
    # Removing controls first prevents an embedded escape from concealing a secret.
    return Controls().strip(redactor.redact(Controls().strip(value)))[:limit]
