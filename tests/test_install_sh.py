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
