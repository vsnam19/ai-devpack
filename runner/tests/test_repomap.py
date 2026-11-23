"""Unit tests for runner/repomap.py deterministic C/C++ code map and ranking."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import repomap  # noqa: E402


def _make_cpp_repo(root: Path) -> None:
    """Create a multi-module C++17 repository with Doxygen headers and sources."""
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    (root / "include" / "alpha_bus.hpp").write_text(
        "/**\n"
        " * @file alpha_bus.hpp\n"
        " * @brief Alpha bus frame scheduler.\n"
        " */\n"
        "#pragma once\n"
        "#include <cstdint>\n"
        "namespace demo {\n"
        "/** @brief Bus status enum. */\n"
        "enum class AlphaStatus : std::uint8_t { Idle = 0U, Running = 1U };\n"
        "/** @brief Manage alpha frame scheduling. */\n"
        "class AlphaScheduler {\n"
        "public:\n"
        "    /** @brief Enqueue one alpha message. */\n"
        "    bool enqueueAlpha(std::uint32_t msgId) noexcept;\n"
        "private:\n"
        "    void secretInternalHelper() noexcept;\n"
        "};\n"
        "}  // namespace demo\n",
        encoding="utf-8",
    )
    (root / "src" / "alpha_bus.cpp").write_text(
        "/**\n * @file alpha_bus.cpp\n * @brief Alpha implementation.\n */\n"
        "#include \"alpha_bus.hpp\"\n"
        "namespace demo {\n"
        "bool AlphaScheduler::enqueueAlpha(std::uint32_t msgId) noexcept {\n"
        "    return msgId != 0U;\n"
        "}\n"
        "}  // namespace demo\n",
        encoding="utf-8",
    )
    (root / "include" / "zeta_crc.hpp").write_text(
        "/**\n"
        " * @file zeta_crc.hpp\n"
        " * @brief CRC calculation utilities.\n"
        " */\n"
        "#pragma once\n"
        "#include <cstdint>\n"
        "namespace demo {\n"
        "/** @brief Compute CRC16 checksum over payload bytes. */\n"
        "std::uint16_t computeZetaChecksum(const std::uint8_t* data, std::uint32_t len) noexcept;\n"
        "}  // namespace demo\n",
        encoding="utf-8",
    )
    (root / "src" / "zeta_crc.cpp").write_text(
        "/**\n * @file zeta_crc.cpp\n * @brief CRC implementation.\n */\n"
        "#include \"zeta_crc.hpp\"\n"
        "namespace demo {\n"
        "namespace {\n"
        "std::uint16_t hiddenStep(std::uint16_t acc) noexcept { return acc; }\n"
        "}  // namespace\n"
        "std::uint16_t computeZetaChecksum(const std::uint8_t* data, std::uint32_t len) noexcept {\n"
        "    (void)data;\n"
        "    return static_cast<std::uint16_t>(hiddenStep(static_cast<std::uint16_t>(len)));\n"
        "}\n"
        "}  // namespace demo\n",
        encoding="utf-8",
    )


class RepoMapTests(unittest.TestCase):
    """Verify determinism, budget compliance, symbol extraction, and task ranking."""

    def test_deterministic_and_excludes_private_or_anonymous_symbols(self) -> None:
        """Repeated runs produce identical Markdown and expose only public symbols + @brief."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_cpp_repo(repo)
            out1 = repomap.generate_repomap(repo, budget=1500)
            out2 = repomap.generate_repomap(repo, budget=1500)
            self.assertEqual(out1, out2)
            self.assertIn("## File Tree", out1)
            self.assertIn("## Symbols", out1)
            self.assertIn("AlphaScheduler", out1)
            self.assertIn("enqueueAlpha", out1)
            self.assertIn("Enqueue one alpha message.", out1)
            self.assertIn("enum class AlphaStatus", out1)
            self.assertIn("computeZetaChecksum", out1)
            self.assertNotIn("secretInternalHelper", out1)
            self.assertNotIn("hiddenStep", out1)

    def test_ranks_file_containing_task_identifier_first(self) -> None:
        """A task naming computeZetaChecksum ranks include/zeta_crc.hpp first under ## Symbols."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _make_cpp_repo(repo)
            task_file = repo / "task.md"
            task_file.write_text(
                "# Task\n\nFix `computeZetaChecksum` overflow on large inputs.\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = repomap.main(["--repo", str(repo), "--task", str(task_file), "--budget", "1500"])
            self.assertEqual(0, rc)
            md = buf.getvalue()
            symbols_section = md.split("## Symbols", 1)[1]
            first_file_heading = next(
                line for line in symbols_section.splitlines() if line.startswith("### ")
            )
            self.assertIn("include/zeta_crc.hpp", first_file_heading)

    def test_budget_truncation_never_exceeds_token_limit(self) -> None:
        """Output stays strictly within --budget tokens even for tight budgets."""
        fixture_repo = REPO_ROOT / "eval" / "fixture"
        for budget in (120, 300, 1500):
            md = repomap.generate_repomap(
                fixture_repo,
                task_text="Investigate LinkStateMachine onHeartbeat recovery against REQ-LINK-005.",
                budget=budget,
            )
            self.assertLessEqual(repomap.estimate_tokens(md), budget, f"budget={budget}")
            self.assertIn("## File Tree", md)

    def test_fixture_ranks_relevant_header_first_and_cli_error_codes(self) -> None:
        """On eval/fixture, REQ-DTC-003 / DtcDebouncer task ranks dtc_debouncer.hpp first."""
        fixture_repo = REPO_ROOT / "eval" / "fixture"
        md = repomap.generate_repomap(
            fixture_repo,
            task_text="Check onOperationCycleEnd behavior against REQ-DTC-003.",
            budget=1500,
        )
        symbols_section = md.split("## Symbols", 1)[1]
        first_heading = next(
            line for line in symbols_section.splitlines() if line.startswith("### ")
        )
        self.assertIn("dtc_debouncer.hpp", first_heading)

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, repomap.main(["--repo", "/nonexistent/path/xyz"]))
            self.assertEqual(2, repomap.main(["--repo", str(fixture_repo), "--budget", "0"]))


if __name__ == "__main__":
    unittest.main()
