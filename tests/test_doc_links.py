# Header: Asserts that every repo path referenced in backticks by the docs exists.
"""Doc cross-reference test: backticked repo paths in docs must exist on disk."""

from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREFIXES = ("enforcement", "eval", "runner", "workflow", "policy", "knowledge", "tests", "learning", "orchestrator", "skilltest")
PATH_RE = re.compile(r"(?<![\w./-])((?:%s)/[A-Za-z0-9_./-]*)" % "|".join(PREFIXES))
SPAN_RE = re.compile(r"`([^`\n]+)`")
DOC_ROOTS = ("policy", "knowledge", "workflow")


def doc_files() -> list[pathlib.Path]:
    """Return README.md, CHANGELOG.md, every doc under the doc directories and every eval markdown file.

    eval/results/ is excluded because it holds generated run output, not maintained docs.
    """
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
    for name in DOC_ROOTS:
        files.extend(p for p in sorted((ROOT / name).rglob("*")) if p.is_file())
    results_dir = ROOT / "eval" / "results"
    files.extend(
        p for p in sorted((ROOT / "eval").rglob("*.md")) if results_dir not in p.parents
    )
    return files


def extract_paths(text: str) -> set[str]:
    """Extract candidate repo paths from backtick spans and fenced code blocks."""
    chunks: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            chunks.append(line)
        else:
            chunks.extend(SPAN_RE.findall(line))
    found: set[str] = set()
    for chunk in chunks:
        for match in PATH_RE.findall(chunk):
            path = match.rstrip(".,:;)")
            if path and path.split("/", 1)[1:] != [""]:
                found.add(path)
    return found


class ExtractPathsTest(unittest.TestCase):
    """Unit tests for the path extractor itself."""

    def test_extracts_inline_and_fenced_paths(self) -> None:
        """Inline spans and fenced lines yield paths; plain prose does not."""
        text = "see `eval/run_eval.py`, plain eval/nothing.py\n```bash\nbash runner/x.sh\n```\n"
        self.assertEqual(extract_paths(text), {"eval/run_eval.py", "runner/x.sh"})

    def test_ignores_prefix_inside_longer_path(self) -> None:
        """A prefix embedded in another path (e.g. `src/eval/x`) is not a repo path."""
        self.assertEqual(extract_paths("`src/eval/x.py`"), set())


class DocLinksTest(unittest.TestCase):
    """Every referenced path must exist in the repository."""

    def test_doc_paths_exist(self) -> None:
        """Collect all references and assert each resolves to an existing file or directory."""
        missing: list[str] = []
        checked = 0
        for doc in doc_files():
            try:
                text = doc.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for ref in sorted(extract_paths(text)):
                if any(ch in ref for ch in "*<>{}"):
                    continue
                checked += 1
                if not (ROOT / ref).exists():
                    missing.append(f"{doc.relative_to(ROOT)}: {ref}")
        self.assertGreater(checked, 10, "extractor found suspiciously few references")
        self.assertEqual(missing, [], "dangling references:\n" + "\n".join(missing))



def python_commands(text: str) -> list[list[str]]:
    """Extract documented Python script commands, joining shell continuations."""
    commands: list[list[str]] = []
    for line in text.replace("\\\n", " ").splitlines():
        match = re.search(r"python3\s+(?:(?:\$DEVPACK_DIR|/opt/devpack)/)?((?:runner|enforcement|eval|learning|skilltest)/[\w/.-]+\.py)(.*)", line)
        if match:
            tail = match.group(2).strip().rstrip("'")
            commands.append([match.group(1), *shlex.split(tail)])
    return commands


class DocumentedCommandsTest(unittest.TestCase):
    """Documented options must be accepted by the script's actual CLI parser."""

    def assert_command_flags(self, commands: list[list[str]]) -> None:
        """Check command options against real --help output without running work."""
        self.assertGreater(len(commands), 0)
        for command in commands:
            script, *args = command
            subcommands = {"devpack_logs.py", "devpack_memory.py", "skilltest.py"}
            prefix = args[:1] if pathlib.Path(script).name in subcommands else []
            proc = subprocess.run(
                [sys.executable, str(ROOT / script), *prefix, "--help"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{command}: {proc.stderr}")
            accepted = set(re.findall(r"--[a-z][a-z0-9-]*", proc.stdout))
            used = {arg.split("=", 1)[0] for arg in args if arg.startswith("--")}
            self.assertLessEqual(used, accepted, f"unsupported flags in {command}")

    def test_readme_command_flags(self) -> None:
        """README examples name only supported flags, including subcommands."""
        self.assert_command_flags(python_commands((ROOT / "README.md").read_text()))

    def test_snake_case_variant_keeps_documentation_checks(self) -> None:
        """The ready variant changes naming while retaining strict checks."""
        from enforcement.lint.preflight_lint import load_config_file

        config = load_config_file(ROOT / "enforcement/lint/preflight.snake_case.example.toml")
        self.assertTrue(config.require_file_doxygen)
        self.assertTrue(config.require_param_direction)
        self.assertTrue(config.naming.check_meaningful)
        self.assertIsNotNone(re.fullmatch(config.naming.parameter_regex, "input_value"))
        self.assertIsNone(re.fullmatch(config.naming.parameter_regex, "inputValue"))

    def test_failure_attribution_covers_all_lessons(self) -> None:
        """Every current lesson has an attribution row, avoiding stale ID ranges."""
        lessons = set(re.findall(r"^## (LSN-\d+)", (ROOT / "knowledge/lessons.md").read_text(), re.MULTILINE))
        attributed = set(re.findall(r"\| `(LSN-\d+)` \|", (ROOT / "policy/failure_attribution.md").read_text()))
        self.assertGreater(len(lessons), 19)
        self.assertEqual(lessons, attributed)

    def test_ci_command_flags(self) -> None:
        """Both CI examples execute supported commands and real lint/gate stages."""
        for name in ("gitlab-ci.yml", "Jenkinsfile"):
            text = (ROOT / "workflow/ci/examples" / name).read_text()
            with self.subTest(example=name):
                self.assert_command_flags(python_commands(text))
                self.assertNotIn("--review-only", text)
                self.assertNotIn("gate.example.toml", text)
                self.assertIn("--all-components", text)
                self.assertNotIn("preflight_lint.py --help", text)


if __name__ == "__main__":
    unittest.main()
