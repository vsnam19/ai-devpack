# Header: Asserts that every repo path referenced in backticks by the docs exists.
"""Doc cross-reference test: backticked repo paths in docs must exist on disk."""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREFIXES = ("enforcement", "eval", "runner", "workflow", "policy", "knowledge", "tests")
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


if __name__ == "__main__":
    unittest.main()
