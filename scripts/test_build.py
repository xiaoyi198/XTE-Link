"""Build guards use temporary Git repositories; no device or private assets."""
import importlib.metadata
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.build_driver import check_requirements
from scripts.check_dependencies import check_dependencies
from scripts.prepare_driver_payload import prepare


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.repositories = []
        for name, key, script, paths in (
            ("open-x4-sdk", "SDK", "apply_sdk_patch.py", (
                "libs/hardware/SDCardManager/include/SDCardManager.h",
                "libs/hardware/SDCardManager/src/SDCardManager.cpp")),
            ("third_party/esp32-ymodem", "DEPENDENCY", "apply_ymodem_patch.py", (
                "components/ymodem/ymodem.c", "components/ymodem/ymodem.h", "components/ymodem/library.json")),
        ):
            directory = self.root / name
            directory.mkdir(parents=True)
            self.git(directory, "init", "-q")
            self.git(directory, "config", "user.name", "Test")
            self.git(directory, "config", "user.email", "test@example.invalid")
            self.git(directory, "config", "core.autocrlf", "false")
            for relative in paths:
                source = directory / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("before\n", encoding="utf-8", newline="\n")
            self.git(directory, "add", ".")
            self.git(directory, "commit", "-qm", "base")
            commit = self.git(directory, "rev-parse", "HEAD").strip()
            changed = directory / paths[0]
            changed.write_text("after\n", encoding="utf-8", newline="\n")
            patch_file = self.root / (script + ".patch")
            patch_file.write_text(self.git(directory, "diff"), encoding="utf-8", newline="\n")
            (self.root / "scripts" / script).write_text(
                f"from pathlib import Path\n{key} = Path({str(directory)!r})\n"
                f"PATCH = Path({str(patch_file)!r})\nCOMMIT = {commit!r}\n", encoding="utf-8")
            self.repositories.append((directory, changed))

    @staticmethod
    def git(directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], text=True)

    def test_applied_patches_and_actual_source_hashes(self):
        result = check_dependencies(self.root)
        self.assertTrue(result["sdk"]["patch_applied"])
        self.assertEqual(len(result["ymodem"]["sources"]), 3)

    def test_missing_patch_fails_without_applying(self):
        directory, changed = self.repositories[0]
        changed.write_text("before\n", encoding="utf-8")
        before = self.git(directory, "status", "--porcelain")
        with self.assertRaisesRegex(RuntimeError, "apply_sdk_patch.py"):
            check_dependencies(self.root)
        self.assertEqual(changed.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(self.git(directory, "status", "--porcelain"), before)

    def test_conflict_preserves_local_change(self):
        changed = self.repositories[1][1]
        changed.write_text("local conflicting change\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "missing or conflicts"):
            check_dependencies(self.root)
        self.assertEqual(changed.read_text(encoding="utf-8"), "local conflicting change\n")

    def test_wrong_commit_rejected(self):
        directory, _ = self.repositories[0]
        self.git(directory, "commit", "--allow-empty", "-qm", "different revision")
        with self.assertRaisesRegex(RuntimeError, "commit differs"):
            check_dependencies(self.root)

    def test_uninitialized_rejected(self):
        script = self.root / "scripts/apply_sdk_patch.py"
        script.write_text(script.read_text(encoding="utf-8").replace("open-x4-sdk", "absent"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "submodule update"):
            check_dependencies(self.root)


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "build.txt"
        self.path.write_text("-r runtime.txt\nbuilder==2.0\n", encoding="utf-8")
        (self.path.parent / "runtime.txt").write_text("runtime==1.0\n", encoding="utf-8")

    def test_only_required_pins_including_nested_file(self):
        with patch("scripts.build_driver.distribution", side_effect=lambda name: SimpleNamespace(
                version={"runtime": "1.0", "builder": "2.0"}[name])) as lookup:
            check_requirements(self.path)
        self.assertEqual(lookup.call_count, 2)

    def test_wrong_version_and_missing_dependency_fail(self):
        with patch("scripts.build_driver.distribution", return_value=SimpleNamespace(version="9.0")):
            with self.assertRaisesRegex(RuntimeError, "expected 1.0"):
                check_requirements(self.path)
        with patch("scripts.build_driver.distribution", side_effect=importlib.metadata.PackageNotFoundError("runtime")):
            with self.assertRaisesRegex(RuntimeError, "Missing build dependency"):
                check_requirements(self.path)


class PayloadTests(unittest.TestCase):
    def test_changed_dependency_rejected_before_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "firmware.bin").write_bytes(b"image")
            dependencies = {name: {"commit": name, "sources": {"file": "old"}}
                            for name in ("sdk", "ymodem")}
            (root / "manifest.json").write_text(json.dumps({
                "sha256": hashlib.sha256(b"image").hexdigest(), "bytes": 5,
                "source_hashes": {}, "sdk_commit": "sdk", "ymodem_commit": "ymodem",
                "dependencies": dependencies}), encoding="utf-8")
            dependencies["sdk"]["sources"]["file"] = "changed"
            with patch("pc_driver.flash.validate_image"), patch(
                    "scripts.check_dependencies.check_dependencies", return_value=dependencies):
                with self.assertRaisesRegex(ValueError, "dependency sources changed"):
                    prepare(root, root)
            self.assertFalse((root / "build").exists())


if __name__ == "__main__":
    unittest.main()
