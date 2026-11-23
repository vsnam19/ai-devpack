"""Tests for context-window lookup in runner/devpack_logs.py.

A configured context_tokens value in engines.toml must be used. The module must be
able to parse TOML (tomllib), otherwise configured limits silently fall back to defaults.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import devpack_logs  # noqa: E402


class ResolveContextWindowTests(unittest.TestCase):
    """Configured context limits override built-in defaults."""

    def test_configured_context_tokens_is_used_over_default(self) -> None:
        """A context_tokens value in engines.toml wins over the built-in default for that engine."""
        self.assertNotEqual(12345, devpack_logs._DEFAULT_ENGINE_CONTEXT_TOKENS["sonnet"])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "engines.toml"
            cfg.write_text("[engines.sonnet]\ncontext_tokens = 12345\n", encoding="utf-8")
            self.assertEqual(12345, devpack_logs._resolve_context_window("sonnet", cfg))

    def test_min_context_tokens_is_used_when_context_tokens_absent(self) -> None:
        """min_context_tokens is honoured when context_tokens is not configured."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "engines.toml"
            cfg.write_text("[engines.local]\nmin_context_tokens = 4096\n", encoding="utf-8")
            self.assertEqual(4096, devpack_logs._resolve_context_window("local", cfg))

    def test_unconfigured_engine_falls_back_to_default(self) -> None:
        """An engine absent from the config file uses the built-in default."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "engines.toml"
            cfg.write_text("[engines.sonnet]\ncontext_tokens = 12345\n", encoding="utf-8")
            self.assertEqual(
                devpack_logs._DEFAULT_ENGINE_CONTEXT_TOKENS["haiku"],
                devpack_logs._resolve_context_window("haiku", cfg),
            )

    def test_tomllib_is_available_to_module(self) -> None:
        """The module exposes a usable tomllib (or the 3.10 fallback is explicitly None)."""
        self.assertTrue(hasattr(devpack_logs, "tomllib"))
        if devpack_logs.tomllib is not None:
            parsed = devpack_logs.tomllib.loads("x = 1\n")
            self.assertEqual({"x": 1}, parsed)


if __name__ == "__main__":
    unittest.main()
