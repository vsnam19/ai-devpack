"""Shared fixture helpers for tests that submit worker briefs through the broker.

The broker runs ``runner/brief_lint.py`` on every orchestrator-submitted brief and
refuses briefs with error findings. ``valid_brief`` produces a minimal brief that
carries every required heading and a non-empty allowed-files list, so tests can
focus on their own behaviour instead of brief structure.
"""

from __future__ import annotations


def valid_brief(goal: str, allowed: list[str]) -> str:
    """Return a Markdown brief that passes brief_lint for the given goal and allowed files.

    The brief contains the required ``Goal``, ``Input``, ``Expected output``,
    ``Constraints`` and ``Gate`` headings plus a ``Files allowed`` list built from
    ``allowed``. Each entry in ``allowed`` must name a file that exists in the
    repository the brief is linted against when it contains a directory separator.
    """
    lines = [
        "# Brief",
        "",
        "## Goal",
        goal,
        "",
        "## Input",
        "- The repository files named in the goal.",
        "",
        "## Expected output",
        "- The change described in Goal, limited to the allowed files.",
        "",
        "## Files allowed",
        *[f"- `{path}`" for path in allowed],
        "",
        "## Constraints",
        "- Do not edit files outside the allowed list.",
        "",
        "## Gate",
        "- `true`",
        "",
    ]
    return "\n".join(lines)
