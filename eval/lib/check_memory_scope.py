#!/usr/bin/env python3
# Header: Enforce trusted memory-sequence scope and protected test lock before execution.
# Exit codes: 0 intact; 1 scope/test violation; 2 usage or invalid sequence metadata.
"""Check a memory task's cumulative allowed files against its external baseline."""

import argparse
import os
from pathlib import Path
import sys

PACK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACK_ROOT))
from enforcement.checks import scope_check, test_lock  # noqa: E402
from runner.agent_exec import _minimal_toml_loads  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Parse trusted sequence metadata and run both checks before any worker code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--sequence", type=Path, required=True)
    args = parser.parse_args(argv)
    base = os.environ.get("DEVPACK_BASELINE_SHA", "")
    if not base:
        parser.error("missing external DEVPACK_BASELINE_SHA")
    try:
        data = _minimal_toml_loads(args.sequence.read_text(encoding="utf-8"))
        allowed = sorted({
            value for key, step in data.items() if key.startswith("step_")
            for value in step.get("allowed_files", [])
        })
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    if not allowed:
        parser.error("sequence has no allowed files")
    common = ["--repo", str(args.workdir), "--base", base]
    if scope_check.main([*common, "--allow", *allowed]):
        return 1
    return test_lock.main([*common, "--protected", "tests/**", "--no-deviation"])


if __name__ == "__main__":
    raise SystemExit(main())
