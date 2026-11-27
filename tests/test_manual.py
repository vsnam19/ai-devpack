"""Offline manual checks shared by unittest discovery and pytest."""

import argparse
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
import re
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_no_project_leaks import find_leaks


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs/manual/index.html"
ENTRY_DIRS = ("runner", "enforcement", "learning", "eval", "skilltest",
              "knowledge/skills/telaf-selinux/scripts")


class Manual(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.code = []
        self.cli = []
        self.current = None
        self.feed(MANUAL.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "code":
            self.current = (attributes, [])

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "code" and self.current is not None:
            attributes, parts = self.current
            value = "".join(parts).strip()
            self.code.append(value)
            if "data-entry" in attributes:
                self.cli.append((attributes["data-entry"], attributes.get("data-command", ""), value))
            self.current = None


class ParserReady(Exception):
    def __init__(self, parser):
        self.parser = parser


def stop_at_parse(parser, *args, **kwargs):
    raise ParserReady(parser)


@contextmanager
def parser_environment(path, temp):
    with patch.object(sys, "path", [str(path.parent), str(ROOT), *sys.path]), \
         patch.object(sys, "argv", [str(path)]), \
         patch.object(sys, "dont_write_bytecode", True), \
         patch.dict("os.environ", {"DEVPACK_PROJECT_CONFIG": "none", "XDG_STATE_HOME": temp}):
        yield


def entry_points():
    candidates = [ROOT / "tests/verify_orchestrator_goals.py"]
    for directory in ENTRY_DIRS:
        candidates.extend((ROOT / directory).rglob("*.py"))
    for path in sorted(candidates):
        if "tests" in path.relative_to(ROOT).parts[:-1] and path.name != "verify_orchestrator_goals.py":
            continue
        if path.name.startswith("devpack_cli"):
            continue
        source = path.read_text(encoding="utf-8")
        if "ArgumentParser(" in source and ".parse_args(" in source:
            yield path
    yield ROOT / "runner/dash/tui/__main__.py"


def parser_for(path, temp):
    with parser_environment(path, temp), patch.object(argparse.ArgumentParser, "parse_args", stop_at_parse):
        try:
            if path == ROOT / "runner/dash/tui/__main__.py":
                runpy.run_module("runner.dash.tui.__main__", run_name="__main__")
            else:
                runpy.run_path(str(path), run_name="__main__")
        except ParserReady as captured:
            return captured.parser
    raise AssertionError(f"No CLI parser reached for {path.relative_to(ROOT)}")


def commands(parser, command=""):
    yield command, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from commands(child, f"{command} {name}".strip())


class ManualTest(unittest.TestCase):
    def test_self_contained(self):
        manual = Manual()
        self.assertTrue(manual.elements)
        for tag, attrs in manual.elements:
            self.assertNotIn(tag, {"script", "link", "iframe", "object", "embed", "img", "base"})
            for key in ("href", "src", "xlink:href"):
                if key in attrs:
                    self.assertTrue(attrs[key].startswith("#"), (tag, attrs))
        content = MANUAL.read_text(encoding="utf-8")
        self.assertNotRegex(content, r"(?i)@import\b|\burl\(\s*['\"]?(?!#)")
        self.assertIn("prefers-color-scheme", content)
        self.assertIn("@media print", content)

    def test_anchors(self):
        elements = Manual().elements
        ids = [attrs["id"] for _, attrs in elements if "id" in attrs]
        links = [attrs["href"][1:] for _, attrs in elements if "href" in attrs]
        self.assertGreaterEqual(len(links), 12)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(set(links) - set(ids))

    def test_repository_paths(self):
        paths = [word for word in Manual().code
                 if re.fullmatch(r"(?:[.\w-]+/)+[.\w-]*/?", word)]
        self.assertTrue(paths)
        for value in paths:
            with self.subTest(path=value):
                self.assertTrue((ROOT / value).exists())

    def test_cli_options_match_parsers(self):
        expected = set()
        with tempfile.TemporaryDirectory() as temp:
            for path in entry_points():
                for command, parser in commands(parser_for(path, temp)):
                    entry = path.relative_to(ROOT).as_posix()
                    expected.add((entry, command, ""))
                    for action in parser._actions:
                        for option in action.option_strings:
                            expected.add((entry, command, option))
        documented = Manual().cli
        self.assertTrue(documented)
        self.assertEqual(len(documented), len(set(documented)))
        self.assertSetEqual(expected, set(documented))

    def test_no_project_leaks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / MANUAL.name).write_bytes(MANUAL.read_bytes())
            self.assertEqual([], find_leaks(root))
