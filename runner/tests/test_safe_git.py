"""Unit tests for runner/safe_git.py hardening, denylist, worktrees, and tamper detection."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner.safe_git import (  # noqa: E402
    GitSecurityError,
    build_safe_git_cmd,
    check_git_metadata_unchanged,
    run_git,
    safe_git_env,
    snapshot_git_metadata,
)


def _init_repo(repo_dir: Path) -> None:
    """Create a clean git repository with one commit inside ``repo_dir``."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    env = safe_git_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "Tester"],
        ["add", "README.md"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=str(repo_dir), env=env, check=True, capture_output=True)


class SafeGitUnitTests(unittest.TestCase):
    """Tests for hardened git command building, config denylist, and metadata snapshots."""

    def test_build_cmd_and_env_include_all_hardened_overrides(self) -> None:
        """Verify hardened -c flags, --no-ext-diff/--no-textconv, and isolated env vars."""
        cmd = build_safe_git_cmd(["diff", "--name-only", "HEAD", "--", "src/a.cpp"], repo_root="/tmp/r")
        self.assertEqual("git", cmd[0])
        joined = " ".join(cmd)
        for flag in (
            "core.fsmonitor=",
            "core.hooksPath=/dev/null",
            "diff.external=",
            "core.sshCommand=",
            "protocol.allow=never",
            "core.pager=cat",
            "core.attributesFile=/dev/null",
        ):
            self.assertIn(flag, joined)
        diff_idx = cmd.index("diff")
        dash_idx = cmd.index("--")
        self.assertIn("--no-ext-diff", cmd[diff_idx:dash_idx])
        self.assertIn("--no-textconv", cmd[diff_idx:dash_idx])

        show_cmd = build_safe_git_cmd(["show", "HEAD:README.md"], repo_root="/tmp/r")
        self.assertIn("--no-ext-diff", show_cmd)
        self.assertIn("--no-textconv", show_cmd)

        env = safe_git_env({"GIT_CONFIG_NOSYSTEM": "0", "GIT_CONFIG_GLOBAL": "/tmp/evil"})
        self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual(os.devnull, env["GIT_CONFIG_GLOBAL"])
        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertEqual("1", env["GIT_ATTR_NOSYSTEM"])

    def test_denylist_blocks_fsmonitor_and_prevents_marker_execution(self) -> None:
        """Adding core.fsmonitor = touch <marker> causes run_git to refuse without creating marker."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            marker = base / "pwned.marker"
            snap = snapshot_git_metadata(repo)

            cfg = repo / ".git" / "config"
            cfg.write_text(
                cfg.read_text(encoding="utf-8") + f"\n[core]\n\tfsmonitor = touch {marker}\n",
                encoding="utf-8",
            )

            # Without snapshot: denylist blocks execution
            with self.assertRaises(GitSecurityError):
                run_git(["status"], repo_root=repo)
            self.assertFalse(marker.exists())

            # With snapshot: tamper + denylist block execution
            with self.assertRaises(GitSecurityError):
                run_git(["status"], repo_root=repo, snapshot=snap)
            self.assertFalse(marker.exists())

            ok, reason = check_git_metadata_unchanged(snap)
            self.assertFalse(ok)
            self.assertIn("git_metadata_tampered", reason)

    def test_denylist_rejects_all_dangerous_config_keys(self) -> None:
        """Every key category in the denylist must be refused by run_git."""
        cases = [
            ("core.hookspath", "[core]\n\thooksPath = /tmp/hooks\n"),
            ("diff.*.command", '[diff "evil"]\n\tcommand = /tmp/evil\n'),
            ("diff.external", "[diff]\n\texternal = /tmp/evil\n"),
            ("filter.*", '[filter "evil"]\n\tclean = /tmp/evil\n'),
            ("core.sshcommand", "[core]\n\tsshCommand = /tmp/evil\n"),
            ("alias.*", "[alias]\n\tco = !/tmp/evil\n"),
            ("core.editor", "[core]\n\teditor = /tmp/evil\n"),
            ("core.pager", "[core]\n\tpager = /tmp/evil\n"),
            ("include.path", "[include]\n\tpath = /tmp/evil.cfg\n"),
            ("includeif.*", '[includeIf "gitdir:/"]\n\tpath = /tmp/evil.cfg\n'),
            ("diff.*.textconv", '[diff "evil"]\n\ttextconv = /tmp/evil\n'),
            ("merge.*.driver", '[merge "evil"]\n\tdriver = /tmp/evil %O %A %B\n'),
            ("credential.*helper", "[credential]\n\thelper = /tmp/evil\n"),
            ("gpg.program", "[gpg]\n\tprogram = /tmp/evil\n"),
            ("uploadpack.*", "[uploadpack]\n\tpackObjectsHook = /tmp/evil\n"),
            ("protocol.*", '[protocol "ext"]\n\tallow = always\n'),
        ]
        for label, snippet in cases:
            with self.subTest(key=label), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                _init_repo(repo)
                cfg = repo / ".git" / "config"
                cfg.write_text(cfg.read_text(encoding="utf-8") + "\n" + snippet, encoding="utf-8")
                with self.assertRaises(GitSecurityError, msg=f"Expected refusal for {label}"):
                    run_git(["rev-parse", "HEAD"], repo_root=repo)

    def test_worktree_and_hooks_tampering_detected(self) -> None:
        """Snapshotting a git worktree detects config and hooks mutations across common gitdir."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            main_repo = base / "main"
            _init_repo(main_repo)
            wt = base / "wt"
            env = safe_git_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            subprocess.run(
                ["git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
                env=env,
                check=True,
                capture_output=True,
            )

            snap = snapshot_git_metadata(wt)
            self.assertTrue((wt / ".git").is_file())
            self.assertEqual(snap.common_dir, (main_repo / ".git").resolve())
            self.assertTrue(check_git_metadata_unchanged(snap)[0])

            # Adding a hook to main_repo/.git/hooks must fail verification on the worktree
            hook = main_repo / ".git" / "hooks" / "post-checkout"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
            ok, reason = check_git_metadata_unchanged(snap)
            self.assertFalse(ok)
            self.assertIn("git_metadata_tampered", reason)
            with self.assertRaises(GitSecurityError):
                run_git(["status"], repo_root=wt, snapshot=snap)

    def test_symlinked_git_config_is_refused(self) -> None:
        """Symlinked .git/config is refused before running git."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            target = base / "external_cfg"
            target.write_text("[core]\n\tbare = false\n", encoding="utf-8")
            cfg = repo / ".git" / "config"
            cfg.unlink()
            cfg.symlink_to(target)
            with self.assertRaises(GitSecurityError):
                run_git(["status"], repo_root=repo)

    def test_main_worktree_config_worktree_filter_is_tampered(self) -> None:
        """A filter added to .git/config.worktree after snapshot is tampering in the main worktree too."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            env = safe_git_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            for args in (
                ["config", "core.repositoryformatversion", "1"],
                ["config", "extensions.worktreeConfig", "true"],
            ):
                subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
            snap = snapshot_git_metadata(repo)
            self.assertEqual(snap.git_dir, snap.common_dir)

            marker = base / "worktree-config.marker"
            (repo / ".git" / "config.worktree").write_text(
                f'[filter "evil"]\n\tclean = touch {marker}\n',
                encoding="utf-8",
            )
            ok, reason = check_git_metadata_unchanged(snap)
            self.assertFalse(ok)
            self.assertIn("git_metadata_tampered", reason)
            self.assertIn("config.worktree", reason)
            with self.assertRaises(GitSecurityError):
                run_git(["status"], repo_root=repo, snapshot=snap)
            self.assertFalse(marker.exists())

    def test_filter_hidden_by_bracket_in_subsection_is_denied_by_git_parser(self) -> None:
        """A subsection containing ']' is normalised by git, so its clean filter still hits the denylist."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            marker = base / "bracket.marker"
            cfg = repo / ".git" / "config"
            cfg.write_text(
                cfg.read_text(encoding="utf-8") + f'\n[filter "evil]"]\n\tclean = touch {marker}\n',
                encoding="utf-8",
            )

            snap = snapshot_git_metadata(repo)
            self.assertIn(("filter.*", "filter.evil].clean", f"touch {marker}"), snap.allowed_denied_keys)

            with self.assertRaises(GitSecurityError) as ctx:
                run_git(["rev-parse", "HEAD"], repo_root=repo)
            self.assertIn("filter.evil].clean", str(ctx.exception))
            self.assertIn("filter.*", str(ctx.exception))
            self.assertFalse(marker.exists())

    def test_info_attributes_change_is_tampered(self) -> None:
        """Adding info/attributes after snapshot is detected even though it cannot run code by itself."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            snap = snapshot_git_metadata(repo)
            self.assertTrue(check_git_metadata_unchanged(snap)[0])

            info = repo / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "attributes").write_text("* filter=evil\n", encoding="utf-8")
            ok, reason = check_git_metadata_unchanged(snap)
            self.assertFalse(ok)
            self.assertIn("git_metadata_tampered", reason)
            self.assertIn("info/attributes", reason)

    def test_hook_content_rewrite_under_same_name_is_tampered(self) -> None:
        """Rewriting an existing hook in place is detected by the full-content digest."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            snap = snapshot_git_metadata(repo)
            self.assertTrue(check_git_metadata_unchanged(snap)[0])

            hook.write_text(f"#!/bin/sh\ntouch {base / 'hook.marker'}\n", encoding="utf-8")
            ok, reason = check_git_metadata_unchanged(snap)
            self.assertFalse(ok)
            self.assertIn("hooks/pre-commit", reason)

    def test_honest_main_worktree_with_worktree_config_is_unchanged(self) -> None:
        """Harmless config.worktree content in the main worktree passes the tamper check and runs git."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _init_repo(repo)
            env = safe_git_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            for args in (
                ["config", "core.repositoryformatversion", "1"],
                ["config", "extensions.worktreeConfig", "true"],
            ):
                subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
            (repo / ".git" / "config.worktree").write_text("[user]\n\tname = Tester\n", encoding="utf-8")
            snap = snapshot_git_metadata(repo)
            self.assertEqual(frozenset(), snap.allowed_denied_keys)
            self.assertEqual((True, ""), check_git_metadata_unchanged(snap))

            result = run_git(["rev-parse", "HEAD"], repo_root=repo, snapshot=snap, text=True)
            self.assertEqual(0, result.returncode)
            self.assertRegex(result.stdout.strip(), r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
