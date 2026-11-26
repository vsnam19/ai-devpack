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
TEXT_LIMIT = 16 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)


def identifier(value):
    return isinstance(value, str) and ".." not in value and bool(IDENTIFIER.fullmatch(value))


def mapping(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else []


@contextmanager
def directory(path):
    """Walk from the filesystem anchor with each directory held by descriptor."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular(path):
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("expected a regular file")
            yield fd, info
        finally:
            os.close(fd)


def names(path, *, folders=False):
    with directory(path) as fd, os.scandir(fd) as entries:
        return sorted(
            entry.name for entry in entries
            if (entry.is_dir if folders else entry.is_file)(follow_symlinks=False)
        )


def stamp(path, *, folder=False):
    if folder:
        with directory(path) as fd:
            info = os.fstat(fd)
    else:
        with regular(path) as (_, info):
            pass
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class DeferredRead(Exception):
    """The poll budget was spent before this read."""


class Files:
    def __init__(self):
        self.remaining = None

    def read(self, path, limit):
        with regular(path) as (fd, info):
            if info.st_size > limit:
                raise ValueError("artifact exceeds read limit")
            if self.remaining is not None and self.remaining < info.st_size + 1:
                raise DeferredRead
            data = os.read(fd, min(limit + 1, info.st_size + 1))
            if self.remaining is not None:
                self.remaining -= len(data)
            if len(data) != info.st_size or os.fstat(fd).st_mtime_ns != info.st_mtime_ns:
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
        return None
    # Removing controls first prevents an embedded escape from concealing a secret.
    return Controls().strip(redactor.redact(Controls().strip(value)))[:limit]
