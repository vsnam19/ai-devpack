"""Exercise release packaging with isolated Git repositories and offline self-tests."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import test_no_project_leaks as leak


ROOT = Path(__file__).resolve().parents[1]


class MakeReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        self.out = Path(temp.name) / "out"
        self.bin = Path(temp.name) / "bin"
        self.bin.mkdir()
        self.env = leak._git_env()
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        self.git("init", "-q", "--template=")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        self.write("VERSION", "1.2.3\n")
        shutil.copy2(ROOT / "install.sh", self.root / "install.sh")
        self.write("tests/run_all.sh", "printf 'ALL OK\\n'\n")
        self.write("tests/test_no_project_leaks.py", "raise SystemExit(42)\n")
        script = self.root / "tools/make_release.sh"
        script.parent.mkdir()
        shutil.copyfile(ROOT / "tools/make_release.sh", script)
        # The production rules run from the checkout, distinct from the historical archive.
        shellcheck = self.bin / "shellcheck"
        shellcheck.symlink_to("/bin/true")
        self.release_env = dict(self.env, PATH=f"{self.bin}{os.pathsep}{self.env['PATH']}")

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True, capture_output=True)

    def tag_snapshot(self, tag="1.2.3"):
        self.git("add", "-A")
        self.git("commit", "-qm", "Snapshot")
        self.git("tag", tag)
        shutil.copyfile(ROOT / "tests/test_no_project_leaks.py", self.root / "tests/test_no_project_leaks.py")

    def release(self, version="1.2.3", **overrides):
        return subprocess.run(
            ["bash", "tools/make_release.sh", version, str(self.out)],
            cwd=self.root, env=dict(self.release_env, **overrides),
            capture_output=True, text=True, check=False,
        )

    def test_tagged_release_checksum_and_archive_scope(self):
        generated = (
            "eval/results/report.txt", "skilltest/results/report.txt",
            "learning/reports/report.txt", "evidence/top.json", "nested/evidence/detail.json",
        )
        for path in generated:
            self.write(path, "generated\n")
        self.write("keep.txt", "committed\n")
        self.tag_snapshot()
        self.write("keep.txt", "working tree only\n")
        self.write(".git/info/attributes", "keep.txt export-ignore\n")
        config = self.out.parent / "global.gitconfig"
        attributes = self.out.parent / "attributes"
        attributes.write_text("keep.txt export-ignore\n")
        config.write_text(f"[core]\nattributesFile = {attributes}\n")
        result = self.release("v1.2.3", GIT_DIR="/missing", GIT_WORK_TREE="/missing",
                              GIT_INDEX_FILE="/missing", GIT_CONFIG_GLOBAL=str(config))
        self.assertEqual(0, result.returncode, result.stderr)
        archive = self.out / "devpack-1.2.3.tar.gz"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(f"RELEASE_OK {digest}", result.stdout.splitlines()[-1])
        self.assertEqual(f"{digest}  {archive.name}\n",
                         (self.out / "devpack-1.2.3.sha256").read_text())
        with tarfile.open(archive) as packed:
            installer = packed.getmember("devpack-1.2.3/install.sh")
            self.assertTrue(installer.isfile())
            self.assertTrue(installer.mode & 0o111)
            self.assertEqual(b"committed\n", packed.extractfile("devpack-1.2.3/keep.txt").read())
            for path in generated:
                self.assertNotIn(f"devpack-1.2.3/{path}", packed.getnames())

    def assert_rejected(self, result, diagnostic):
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(diagnostic, result.stderr)
        self.assertNotIn("RELEASE_OK", result.stdout)
        self.assertFalse(self.out.exists())

    def test_missing_tag_refuses_head(self):
        self.tag_snapshot()
        self.assert_rejected(self.release("1.2.4"), "No release tag found")

    def test_tag_version_mismatch_is_rejected(self):
        self.tag_snapshot("v1.2.4")
        self.assert_rejected(self.release("1.2.4"), "Archived VERSION does not match")

    def test_missing_shellcheck_is_reported(self):
        result = subprocess.run(
            [shutil.which("bash"), "tools/make_release.sh", "1.2.3", str(self.out)],
            cwd=self.root, env=dict(self.env, PATH=""), capture_output=True, text=True, check=False,
        )
        self.assert_rejected(result, "requires shellcheck")

    def test_leaks_in_archive_are_rejected(self):
        word = next(term for term in leak.FORBIDDEN if "_" in term)
        host_path = "/Us" + "ers/alice"
        self.write("notes.txt", f'path="{host_path}"\n')
        self.write("commit.txt", "Co-authored-by: " + "Co" + "dex\n")
        (self.root / "proxy.link").symlink_to(f"../{word}/missing")
        (self.root / f"{word}.link").symlink_to("../missing")
        self.tag_snapshot()
        result = self.release()
        self.assert_rejected(result, f"proxy.link: (link target) {word}")
        self.assertIn(f"{word}.link: (file name) {word}", result.stderr)

        self.git("rm", "proxy.link", f"{word}.link")
        self.git("commit", "-qm", "Remove project terms")
        result = self.release("0.0.0-test")
        self.assert_rejected(result, "notes.txt:1: absolute home path in content")
        self.assertIn("commit.txt:1: AI attribution in content", result.stderr)

    def test_home_symlink_target_without_trailing_slash_is_rejected(self):
        (self.root / "account.link").symlink_to("/Us" + "ers/alice")
        self.tag_snapshot("v1.2.3")
        self.assert_rejected(self.release(), "account.link:1: absolute home path in link target")
