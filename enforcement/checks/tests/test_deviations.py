"""Tests for the external human deviation registry loader (enforcement/checks/deviations.py).

Covers extension-based format selection and the exact TOML example documented in
policy/deviations.md, which must parse and verify.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ENFORCEMENT_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = ENFORCEMENT_DIR.parent
for _p in (str(REPO_ROOT), str(ENFORCEMENT_DIR / "checks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deviations  # noqa: E402

POLICY_DOC = REPO_ROOT / "policy" / "deviations.md"
REFERENCE_NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def _documented_toml_example() -> str:
    """Return the first fenced ```toml block from policy/deviations.md verbatim."""
    text = POLICY_DOC.read_text(encoding="utf-8")
    start = text.index("```toml\n") + len("```toml\n")
    end = text.index("```", start)
    return text[start:end]


class DeviationRegistryFormatTests(unittest.TestCase):
    """The registry format is chosen by file extension."""

    def test_documented_toml_example_parses_and_verifies(self) -> None:
        """The exact TOML example from policy/deviations.md loads and verifies for its file."""
        example = _documented_toml_example()
        self.assertIn("[[deviations]]", example)
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "deviations.toml"
            reg.write_text(example, encoding="utf-8")
            entries, err = deviations.load_deviations_registry(reg)
            self.assertIsNone(err)
            assert entries is not None
            self.assertEqual({"DEV-001"}, set(entries))
            entry = entries["DEV-001"]
            self.assertEqual("tech-lead", entry.approved_by)
            self.assertEqual(("eval/fixture/tests/test_can_codec.cpp",), entry.files)
            self.assertEqual(("test_lock",), entry.rules)

            verified, why = deviations.verify_deviation(
                "DEV-001",
                files=["eval/fixture/tests/test_can_codec.cpp"],
                rule="test_lock",
                registry_path=reg,
                now=REFERENCE_NOW,
            )
            self.assertIsNotNone(verified, why)
            self.assertEqual("", why)

    def test_json_registry_with_top_level_list_parses(self) -> None:
        """A .json registry whose content starts with '[' is parsed as JSON."""
        content = (
            '[{"id": "DEV-900", "files": ["tests/test_a.py"], "rules": ["test_lock"], '
            '"approved_by": "lead", "expires": "2027-01-01"}]'
        )
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "deviations.json"
            reg.write_text(content, encoding="utf-8")
            entries, err = deviations.load_deviations_registry(reg)
        self.assertIsNone(err)
        assert entries is not None
        self.assertEqual("lead", entries["DEV-900"].approved_by)

    def test_toml_extension_is_not_parsed_as_json(self) -> None:
        """A .toml registry with a leading '[[' is read as TOML, not rejected as bad JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "registry.toml"
            reg.write_text(
                '[[deviations]]\nid = "DEV-002"\nfiles = ["a.py"]\nrules = ["test_lock"]\n'
                'approved_by = "lead"\nexpires = "2027-01-01"\n',
                encoding="utf-8",
            )
            entries, err = deviations.load_deviations_registry(reg)
        self.assertIsNone(err)
        assert entries is not None
        self.assertIn("DEV-002", entries)

    def test_ruleless_entry_is_rejected(self) -> None:
        """Registry entries omitting the required rules field are rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "deviations.json"
            reg.write_text(
                '{"deviations":[{"id":"DEV-NORULE","files":["src/a.cpp"],'
                '"approved_by":"lead","expires":"2099-12-31"}]}',
                encoding="utf-8",
            )
            entries, err = deviations.load_deviations_registry(reg)
        self.assertIsNone(entries)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("rules", err)

    def test_unsupported_extension_is_refused(self) -> None:
        """A registry with an extension other than .toml or .json is refused with a clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "deviations.yaml"
            reg.write_text("deviations: []\n", encoding="utf-8")
            entries, err = deviations.load_deviations_registry(reg)
        self.assertIsNone(entries)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("unsupported deviation registry extension", err)


if __name__ == "__main__":
    unittest.main()
