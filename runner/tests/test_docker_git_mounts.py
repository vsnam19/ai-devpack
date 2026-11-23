"""Tests for the Docker ``.git`` bind-mount policy in runner/agent_exec.py (T24).

Repository content controls ``.git``, so the host paths bind-mounted into the container
must never be chosen by that content. A ``.git`` pointer file is mounted only when its
gitdir equals the ``trusted_git_dir`` the harness supplies; otherwise no git metadata is
mounted and the reason is recorded. These tests use real git repositories and worktrees
in temporary directories; they make no network or model calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_exec  # noqa: E402
from agent_exec import (  # noqa: E402
    AgentRequest,
    EngineConfig,
    HarnessError,
    build_invocation,
    docker_git_mount_decision,
)

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd`` with an isolated environment; raise on failure."""
    subprocess.run(["git", *args], cwd=str(cwd), env=_GIT_ENV, check=True, capture_output=True)


def _make_repo(repo: Path) -> None:
    """Create a git repository with one commit at ``repo``."""
    repo.mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "Tester"], repo)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], repo)


def _docker_argv(workdir: Path, base: Path, trusted: Path | None = None) -> list[str]:
    """Return the docker argv built for ``workdir`` in docker isolation."""
    req = AgentRequest(
        engine=EngineConfig(name="haiku", model="haiku", engine_class="cloud"),
        prompt="mount policy check",
        workdir=workdir,
        run_dir=base / "runs" / "mount-check",
        isolation="docker",
        role="implementer",
        trusted_git_dir=trusted,
    )
    argv, _env = build_invocation(req)
    return list(argv)


def _mount_specs(argv: list[str]) -> list[str]:
    """Return every ``-v`` value from a docker argv, in order."""
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]


def _write_fake_docker(path: Path) -> Path:
    """Write an executable stand-in for ``docker`` that prints nothing and exits 0."""
    path.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class DockerGitMountPolicyTests(unittest.TestCase):
    """Mount-source policy for ``.git`` (directory, worktree pointer, symlink) in docker mode."""

    def test_dot_git_symlink_is_refused(self) -> None:
        """A ``.git`` symlink to a host directory outside the workdir is refused with no mount."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside-git"
            outside.mkdir()
            (outside / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            work = base / "work"
            work.mkdir()
            os.symlink(outside, work / ".git")
            with self.assertRaises(HarnessError) as ctx:
                _docker_argv(work, base)
            self.assertIn("symlink", str(ctx.exception))

    def test_worktree_pointer_to_outside_dir_is_not_mounted(self) -> None:
        """A ``.git`` file with ``gitdir:`` to a plain outside dir mounts nothing and records why."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside-gitdir"
            outside.mkdir()
            work = base / "work"
            work.mkdir()
            (work / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
            argv = _docker_argv(work, base)
            mounts = _mount_specs(argv)
            self.assertFalse(any(str(outside) in spec for spec in mounts), mounts)
            self.assertFalse(any("/workspace/task/.git:" in spec for spec in mounts), mounts)
            decision = docker_git_mount_decision(work, None)
            self.assertFalse(decision.mounted)
            self.assertTrue(decision.reason)

    def test_worktree_pointer_to_crafted_outside_gitdir_is_not_mounted(self) -> None:
        """A crafted outside gitdir with HEAD and commondir is never mounted, even if it looks valid."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            crafted = base / "crafted-gitdir"
            crafted.mkdir()
            (crafted / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (crafted / "commondir").write_text(".\n", encoding="utf-8")
            work = base / "work"
            work.mkdir()
            (work / ".git").write_text(f"gitdir: {crafted}\n", encoding="utf-8")
            mounts = _mount_specs(_docker_argv(work, base))
            self.assertFalse(any(str(crafted) in spec for spec in mounts), mounts)
            self.assertFalse(any(str(repo) in spec for spec in mounts), mounts)

    def test_worktree_pointer_to_main_gitdir_is_not_mounted(self) -> None:
        """A pointer to the main repository gitdir is never mounted, even with a trusted path set."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            work = base / "work"
            work.mkdir()
            (work / ".git").write_text(f"gitdir: {repo / '.git'}\n", encoding="utf-8")
            mounts = _mount_specs(_docker_argv(work, base, trusted=(repo / ".git").resolve()))
            self.assertFalse(any("/workspace/task/.git:" in spec for spec in mounts), mounts)
            self.assertFalse(any(str((repo / ".git").resolve()) in spec for spec in mounts), mounts)

    def test_worktree_pointer_to_other_repo_worktree_is_not_mounted(self) -> None:
        """A valid worktree of another repository is not mounted when the trusted path names a different one."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_a = base / "repo-a"
            repo_b = base / "repo-b"
            _make_repo(repo_a)
            _make_repo(repo_b)
            wt_a = base / "wt-a"
            wt_b = base / "wt-b"
            _git(["worktree", "add", "-q", "-b", "wt-a", str(wt_a)], repo_a)
            _git(["worktree", "add", "-q", "-b", "wt-b", str(wt_b)], repo_b)
            victim_gitdir = (repo_a / ".git" / "worktrees" / "wt-a").resolve()
            trusted_b = (repo_b / ".git" / "worktrees" / "wt-b").resolve()
            # The checkout that will run is wt-b, but its .git points at repo A's worktree.
            (wt_b / ".git").write_text(f"gitdir: {victim_gitdir}\n", encoding="utf-8")

            argv = _docker_argv(wt_b, base, trusted=trusted_b)
            mounts = _mount_specs(argv)

            self.assertFalse(any(str(repo_a.resolve() / ".git") in spec for spec in mounts), mounts)
            self.assertFalse(any(str(victim_gitdir) in spec for spec in mounts), mounts)
            self.assertFalse(any("/workspace/task/.git:" in spec for spec in mounts), mounts)
            decision = docker_git_mount_decision(wt_b, trusted_b)
            self.assertFalse(decision.mounted)
            self.assertIn("trusted", decision.reason)

    def test_real_worktree_without_trusted_path_mounts_no_git_metadata(self) -> None:
        """A valid worktree pointer is not mounted when the harness supplied no trusted path."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            wt = base / "wt"
            _git(["worktree", "add", "-q", "-b", "wt-none", str(wt)], repo)
            wt_gitdir = (repo / ".git" / "worktrees" / "wt").resolve()
            common = (repo / ".git").resolve()

            mounts = _mount_specs(_docker_argv(wt, base))

            self.assertFalse(any("/workspace/task/.git:" in spec for spec in mounts), mounts)
            self.assertFalse(any(str(wt_gitdir) in spec for spec in mounts), mounts)
            self.assertFalse(any(str(common) in spec for spec in mounts), mounts)
            decision = docker_git_mount_decision(wt, None)
            self.assertFalse(decision.mounted)
            self.assertIn("no trusted git dir", decision.reason)

    def test_real_worktree_is_accepted_with_read_only_gitdir_and_common_dir(self) -> None:
        """A real ``git worktree add`` checkout mounts its gitdir and the common dir, both read-only."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            wt = base / "wt"
            _git(["worktree", "add", "-q", "-b", "wt-t24", str(wt)], repo)
            wt_gitdir = (repo / ".git" / "worktrees" / "wt").resolve()
            common = (repo / ".git").resolve()

            mounts = _mount_specs(_docker_argv(wt, base, trusted=wt_gitdir))

            self.assertIn(f"{wt / '.git'}:/workspace/task/.git:ro", mounts)
            self.assertIn(f"{wt_gitdir}:{wt_gitdir}:ro", mounts)
            self.assertIn(f"{common}:{common}:ro", mounts)
            for spec in mounts:
                if str(spec).startswith((str(wt_gitdir), str(common), str(wt / ".git"))):
                    self.assertTrue(spec.endswith(":ro"), f"git mount must be read-only: {spec}")
            decision = docker_git_mount_decision(wt, wt_gitdir)
            self.assertTrue(decision.mounted)
            self.assertEqual("", decision.reason)

    def test_trusted_path_that_is_not_a_worktree_gitdir_mounts_nothing(self) -> None:
        """A trusted path that is the main gitdir (not ``worktrees/<name>``) yields no git mount."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            wt = base / "wt"
            _git(["worktree", "add", "-q", "-b", "wt-bad", str(wt)], repo)
            main_gitdir = (repo / ".git").resolve()
            (wt / ".git").write_text(f"gitdir: {main_gitdir}\n", encoding="utf-8")

            decision = docker_git_mount_decision(wt, main_gitdir)

            self.assertFalse(decision.mounted)
            self.assertEqual([], decision.mounts)
            self.assertTrue(decision.reason)

    def test_plain_clone_dot_git_directory_is_mounted_read_only(self) -> None:
        """A real ``.git`` directory directly inside the workdir is mounted read-only at the task path."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _make_repo(repo)
            mounts = _mount_specs(_docker_argv(repo, base))
            self.assertIn(f"{repo.resolve() / '.git'}:/workspace/task/.git:ro", mounts)

    def test_run_agent_records_git_metadata_not_mounted_in_run_json(self) -> None:
        """``run_agent`` writes ``git_metadata_mounted: false`` and a reason to run.json for a foreign pointer."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_a = base / "repo-a"
            repo_b = base / "repo-b"
            _make_repo(repo_a)
            _make_repo(repo_b)
            wt_a = base / "wt-a"
            wt_b = base / "wt-b"
            _git(["worktree", "add", "-q", "-b", "wt-a", str(wt_a)], repo_a)
            _git(["worktree", "add", "-q", "-b", "wt-b", str(wt_b)], repo_b)
            victim_gitdir = (repo_a / ".git" / "worktrees" / "wt-a").resolve()
            trusted_b = (repo_b / ".git" / "worktrees" / "wt-b").resolve()
            (wt_b / ".git").write_text(f"gitdir: {victim_gitdir}\n", encoding="utf-8")
            run_dir = base / "runs" / "t24-record"
            docker = _write_fake_docker(base / "fake_docker.py")

            agent_exec.run_agent(
                AgentRequest(
                    engine=EngineConfig(name="haiku", model="haiku", engine_class="cloud"),
                    prompt="record mount decision",
                    workdir=wt_b,
                    run_dir=run_dir,
                    isolation="docker",
                    role="implementer",
                    docker_bin=str(docker),
                    trusted_git_dir=trusted_b,
                    timeout_s=30,
                )
            )

            run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertIs(False, run_json["git_metadata_mounted"])
            self.assertIn("trusted", run_json["git_metadata_reason"])

    def test_path_with_colon_is_refused(self) -> None:
        """A workdir whose path contains ``:`` is refused before any docker argv is built."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "odd:dir"
            _make_repo(repo)
            with self.assertRaises(HarnessError) as ctx:
                _docker_argv(repo, base)
            self.assertIn("':'", str(ctx.exception))

    def test_mount_source_validator_rejects_separator_characters(self) -> None:
        """``:``, ``,`` and newline in a mount source are each refused; a plain path is returned unchanged."""
        for bad in ("/tmp/a:b", "/tmp/a,b", "/tmp/a\nb", "/tmp/a\rb"):
            with self.subTest(path=bad):
                with self.assertRaises(HarnessError):
                    agent_exec._validate_mount_source(bad)
        self.assertEqual("/tmp/plain-dir", agent_exec._validate_mount_source(Path("/tmp/plain-dir")))


if __name__ == "__main__":
    unittest.main()
