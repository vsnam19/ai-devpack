"""Exercise the installer using a minimal offline release."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "devpack-1.2.3"
        self.source.mkdir()
        self.env = dict(os.environ, HOME=str(self.base / "home"), PYTHONDONTWRITEBYTECODE="1")
        for name in ("install.sh", "runner/devpack_cli.py"):
            dest = self.source / name
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        (self.source / "VERSION").write_text("1.2.3\n")
        (self.source / "tests").mkdir()
        (self.source / "tests/run_all.sh").write_text("printf 'ALL OK\\n'\n")

    def install(self, *args):
        return subprocess.run(
            ["bash", str(self.source / "install.sh"), *args], cwd=self.base,
            env=self.env, text=True, capture_output=True,
        )

    def test_default_install_reinstall_and_uninstall(self):
        launcher = Path(self.env["HOME"]) / ".local/bin/devpack"
        prefix = Path(self.env["HOME"]) / ".local/share/devpack/1.2.3"
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ALL OK", result.stdout)
            self.assertEqual((prefix.parent / "current").resolve(), prefix)
            result = subprocess.run([launcher, "version"], cwd=self.base,
                                    env=self.env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1.2.3")
        extra = prefix / "local-notes"
        extra.write_text("keep")
        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(launcher.is_symlink())
        self.assertFalse((prefix.parent / "current").is_symlink())
        self.assertFalse((prefix / "VERSION").exists())
        self.assertEqual(extra.read_text(), "keep")

    def test_checksum_and_custom_locations(self):
        archive = self.base / (self.source.name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.source, arcname=self.source.name)
        checksum = self.base / (self.source.name + ".sha256")
        prefix = self.base / "custom prefix"
        bin_dir = self.base / "custom bin"
        options = ("--prefix", str(prefix), "--bin-dir", str(bin_dir),
                   "--verify", str(checksum), "--no-selftest")
        checksum.write_text(f"{'0' * 64}  {archive.name}\n")
        result = self.install(*options)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(prefix.exists())
        self.assertFalse(bin_dir.exists())
        checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        result = self.install(*options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ALL OK", result.stdout)
        self.assertEqual((prefix / "VERSION").read_text(), "1.2.3\n")
        self.assertTrue((bin_dir / "devpack").is_symlink())
        result = self.install(*options[:4], "--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(prefix.exists())
        self.assertFalse((bin_dir / "devpack").is_symlink())

    def test_preserves_unrelated_launcher_and_prefix(self):
        prefix = self.base / "prefix"
        bin_dir = self.base / "bin"
        prefix.mkdir()
        bin_dir.mkdir()
        launcher = bin_dir / "devpack"
        launcher.write_text("owned by user")
        sentinel = prefix / "sentinel"
        sentinel.write_text("keep")
        options = ("--prefix", str(prefix), "--bin-dir", str(bin_dir))
        self.assertNotEqual(self.install(*options, "--no-selftest").returncode, 0)
        self.assertNotEqual(self.install(*options, "--uninstall").returncode, 0)
        self.assertEqual(launcher.read_text(), "owned by user")
        self.assertEqual(sentinel.read_text(), "keep")

    def test_uninstall_cleans_pycache_and_keeps_outside_files(self):
        prefix = Path(self.env["HOME"]) / ".local/share/devpack/1.2.3"
        launcher = Path(self.env["HOME"]) / ".local/bin/devpack"
        outside = prefix.parent / "outside-keep"
        clean_env = {k: v for k, v in self.env.items() if k != "PYTHONDONTWRITEBYTECODE"}

        # Helper module so importing it without bytecode suppression would write __pycache__.
        (self.source / "runner/helper_mod.py").write_text("VALUE = 1\n")
        (self.source / "tests/run_all.sh").write_text(
            "python3 -c 'import sys; sys.path.insert(0, \"runner\"); import helper_mod'\n"
            "printf 'ALL OK\\n'\n"
        )
        res = subprocess.run(
            ["bash", str(self.source / "install.sh")], cwd=self.base,
            env=clean_env, text=True, capture_output=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        outside.write_text("keep")

        # Running devpack commands in a standard shell must not write __pycache__ in $prefix.
        for args in (["version"], ["selftest"], ["doctor", "--workdir", str(self.base)]):
            proc = subprocess.run([launcher, *args], cwd=self.base,
                                  env=clean_env, text=True, capture_output=True)
            if args[0] != "doctor":
                self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((prefix / "runner/__pycache__").exists())

        # Even if a __pycache__ directory is created manually inside managed paths, uninstall removes it.
        cache_dir = prefix / "runner/__pycache__"
        cache_dir.mkdir()
        (cache_dir / "helper_mod.cpython-312.pyc").write_bytes(b"pyc")
        (prefix / "__pycache__").mkdir()
        (prefix / "__pycache__/top.pyc").write_bytes(b"pyc")

        res = subprocess.run(
            ["bash", str(self.source / "install.sh"), "--uninstall"], cwd=self.base,
            env=clean_env, text=True, capture_output=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(prefix.exists())
        self.assertEqual(outside.read_text(), "keep")

        # Reinstalling the same version works cleanly after uninstall.
        res = subprocess.run(
            ["bash", str(self.source / "install.sh"), "--no-selftest"], cwd=self.base,
            env=clean_env, text=True, capture_output=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((prefix / "VERSION").is_file())

    def test_uninstall_fails_preserving_manifest_when_managed_file_cannot_be_removed(self):
        prefix = Path(self.env["HOME"]) / ".local/share/devpack/1.2.3"
        res = self.install("--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        runner_dir = prefix / "runner"
        runner_dir.chmod(0o555)
        self.addCleanup(lambda: runner_dir.exists() and runner_dir.chmod(0o755))
        res = self.install("--uninstall")
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue((prefix / ".devpack-installed-files").is_file())

    def test_moved_prefixes_sibling_versions_and_dangling_links(self):
        home = Path(self.env["HOME"])
        launcher = home / ".local/bin/devpack"
        current = home / ".local/share/devpack/current"
        prefix_100 = home / ".local/share/devpack/1.0.0"
        prefix_123 = home / ".local/share/devpack/1.2.3"

        (self.source / "VERSION").write_text("1.0.0\n")
        self.assertEqual(self.install("--no-selftest").returncode, 0)
        (self.source / "VERSION").write_text("1.2.3\n")
        res = self.install("--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(current.resolve(), prefix_123)

        # Uninstalling 1.2.3 repoints current to remaining sibling 1.0.0 and preserves launcher.
        res = self.install("--uninstall")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(prefix_123.exists())
        self.assertEqual(current.resolve(), prefix_100)
        self.assertTrue(launcher.is_symlink())
        ver = subprocess.run([launcher, "version"], cwd=self.base, env=self.env, text=True, capture_output=True)
        self.assertEqual(ver.stdout.strip(), "1.0.0")

        # Switching from default prefix to custom --prefix updates launcher, and between custom prefixes works.
        custom_a = self.base / "opt/devpack-1.2.3"
        custom_b = self.base / "opt/devpack-2.0.0"
        res = self.install("--prefix", str(custom_a), "--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.readlink(launcher), str(custom_a / "runner/devpack_cli.py"))

        (self.source / "VERSION").write_text("2.0.0\n")
        res = self.install("--prefix", str(custom_b), "--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.readlink(launcher), str(custom_b / "runner/devpack_cli.py"))

        # Dangling launcher and dangling current link to deleted sibling are both recoverable.
        shutil.rmtree(custom_b)
        shutil.rmtree(prefix_100)
        self.assertTrue(launcher.is_symlink() and not launcher.exists())
        self.assertTrue(current.is_symlink() and not current.exists())

        (self.source / "VERSION").write_text("1.2.3\n")
        res = self.install("--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(current.resolve(), prefix_123)
        self.assertEqual(os.readlink(launcher), str(current / "runner/devpack_cli.py"))

        # Unrelated launcher symlink outside devpack root is still refused.
        other_target = self.base / "other/tool"
        other_target.parent.mkdir()
        other_target.write_text("#!/bin/sh\n")
        launcher.unlink()
        launcher.symlink_to(other_target)
        res = self.install("--prefix", str(custom_a), "--no-selftest")
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(os.readlink(launcher), str(other_target))

    def test_verify_errors_and_no_trailing_newline(self):
        prefix = self.base / "verify-prefix"
        bin_dir = self.base / "verify-bin"
        base_opts = ("--prefix", str(prefix), "--bin-dir", str(bin_dir), "--no-selftest")

        # Missing checksum file.
        res = self.install(*base_opts, "--verify", str(self.base / "missing.sha256"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("checksum", res.stderr.lower())

        # Empty checksum file.
        empty_sum = self.base / "devpack-1.2.3.sha256"
        empty_sum.write_text("")
        res = self.install(*base_opts, "--verify", str(empty_sum))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid release checksum file", res.stderr)

        # Malformed checksum file.
        empty_sum.write_text("not-a-hash")
        res = self.install(*base_opts, "--verify", str(empty_sum))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid release checksum file", res.stderr)

        # Checksum belonging to a different version.
        old_src = self.base / "devpack-1.2.2"
        shutil.copytree(self.source, old_src)
        (old_src / "VERSION").write_text("1.2.2\n")
        old_archive = self.base / "devpack-1.2.2.tar.gz"
        with tarfile.open(old_archive, "w:gz") as tar:
            tar.add(old_src, arcname=old_src.name)
        old_sum = self.base / "devpack-1.2.2.sha256"
        old_sum.write_text(f"{hashlib.sha256(old_archive.read_bytes()).hexdigest()}  {old_archive.name}\n")
        res = self.install(*base_opts, "--verify", str(old_sum))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid release checksum file", res.stderr)

        # Valid checksum without a trailing newline succeeds.
        archive = self.base / "devpack-1.2.3.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(self.source, arcname=self.source.name)
        valid_sum = self.base / "devpack-1.2.3.sha256"
        valid_sum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}")
        res = self.install(*base_opts, "--verify", str(valid_sum))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((prefix / "VERSION").is_file())

    def test_reinstall_restores_deleted_and_modified_files(self):
        prefix = Path(self.env["HOME"]) / ".local/share/devpack/1.2.3"
        res = self.install("--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)

        cli_copy = prefix / "runner/devpack_cli.py"
        run_all_copy = prefix / "tests/run_all.sh"
        cli_copy.unlink()
        run_all_copy.write_text("corrupted\n")

        res = self.install("--no-selftest")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(cli_copy.is_file())
        self.assertEqual(cli_copy.read_text(), (self.source / "runner/devpack_cli.py").read_text())
        self.assertEqual(run_all_copy.read_text(), (self.source / "tests/run_all.sh").read_text())
        manifest_items = (prefix / ".devpack-installed-files").read_bytes().split(b"\0")
        self.assertIn(b"./runner/devpack_cli.py", manifest_items)
