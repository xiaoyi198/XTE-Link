"""Install safety: interrupted file replacement, layout refusal, backup and writes."""

from contextlib import nullcontext
from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zlib
import struct

from pc_driver.flash import inspect_layout, partition_parser, flash_device, FLASH_SIZE
from pc_driver.install import Bundle, Installer, install_file, prepare_fonts, same_file
from pc_driver.tray import TrayApp

ROOT = Path(__file__).resolve().parents[1]
PARSER = ROOT / ".platformio/packages/framework-arduinoespressif32/tools/gen_esp32part.py"


class Card:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.progress = None
        self.uploads = 0
        self.moves = []
        self.fail_move = None

    def request(self, op, path=None, **fields):
        if op == "stat":
            if path not in self.files:
                return {"exists": False}
            data = self.files[path]
            return {"exists": True, "dir": False, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if op == "remove":
            del self.files[path]
        if op == "rename":
            self.moves.append((path, fields["to"]))
            if self.fail_move == len(self.moves):
                raise OSError("simulated USB error")
            assert fields["to"] not in self.files
            self.files[fields["to"]] = self.files.pop(path)
        return {}

    def upload(self, local, remote):
        self.uploads += 1
        self.files[remote] = Path(local).read_bytes()


def fixture_layout():
    parser = partition_parser(PARSER)
    table = parser.PartitionTable.from_csv("nvs,data,nvs,0x9000,0x5000\notadata,data,ota,0xe000,0x2000\n"
                                          "app0,app,ota_0,0x10000,0x640000\napp1,app,ota_1,0x650000,0x640000\n")
    data = bytearray(b"\xff" * 0x10000)
    data[0x8000:0x8c00] = table.to_binary()
    for offset, seq in ((0xe000, 1), (0xf000, 2)):
        data[offset:offset+4] = struct.pack("<I", seq)
        data[offset+24:offset+32] = struct.pack("<II", 2, zlib.crc32(struct.pack("<I", seq), 0xffffffff))
    profile = {"partition_sha256": hashlib.sha256(data[0x8000:0x9000]).hexdigest(),
               "boot_sha256": hashlib.sha256(data[:0x8000]).hexdigest()}
    return bytes(data), parser, profile


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "font"
        self.local.write_bytes(b"new font")
        self.expected = {"tf_path": "/fonts/test.epdfont", "bytes": 8,
                         "sha256": hashlib.sha256(b"new font").hexdigest()}
        self.target = self.expected["tf_path"]

    def tearDown(self):
        self.temp.cleanup()

    def install(self, card, cancel=lambda: None):
        return install_file(card, self.local, self.expected, lambda *args: None, cancel)

    def test_replace_keeps_user_files_and_second_run_skips(self):
        card = Card({self.target: b"old font", "/fonts/Zheng20号.epdfont": b"legacy", "/.sleep/user.bmp": b"user"})
        self.assertTrue(self.install(card))
        self.assertEqual(card.files, {self.target: b"new font", "/fonts/Zheng20号.epdfont": b"legacy", "/.sleep/user.bmp": b"user"})
        self.assertFalse(self.install(card))
        self.assertEqual(card.uploads, 1)

    def test_resume_after_old_renamed_reuses_verified_candidate(self):
        card = Card({self.target + ".xte-old": b"old", self.target + ".xte-next": b"new font"})
        self.install(card)
        self.assertEqual(card.files, {self.target: b"new font"})
        self.assertEqual(card.uploads, 0)

    def test_resume_after_commit_cleans_old_and_partial(self):
        card = Card({self.target: b"new font", self.target + ".xte-old": b"old",
                     self.target + ".xte-next.xte-part": b"partial"})
        self.assertFalse(self.install(card))
        self.assertEqual(card.files, {self.target: b"new font"})

    def test_failed_commit_restores_previous_target(self):
        card = Card({self.target: b"old"})
        card.fail_move = 2
        with self.assertRaises(OSError):
            self.install(card)
        self.assertEqual(card.files[self.target], b"old")
        self.assertNotIn(self.target + ".xte-old", card.files)
        self.install(card)
        self.assertEqual(card.files, {self.target: b"new font"})
        self.assertEqual(card.uploads, 1)

    def test_partial_upload_restarts_without_touching_old_file(self):
        card = Card({self.target: b"old", self.target + ".xte-next.xte-part": b"partial"})
        original = card.upload
        def fail(*args):
            self.assertEqual(card.files[self.target], b"old")
            raise OSError("full card")
        card.upload = fail
        with self.assertRaisesRegex(OSError, "full card"):
            self.install(card)
        self.assertEqual(card.files, {self.target: b"old"})
        card.upload = original
        self.install(card)

    def test_cancel_never_removes_existing_file(self):
        card = Card({self.target: b"old"})
        def cancel():
            raise InterruptedError()
        with self.assertRaises(InterruptedError):
            self.install(card, cancel)
        self.assertEqual(card.files, {self.target: b"old"})

    def test_conflicting_backup_is_preserved(self):
        card = Card({self.target: b"unknown", self.target + ".xte-old": b"precious"})
        with self.assertRaisesRegex(OSError, "冲突"):
            self.install(card)
        self.assertEqual(card.files[self.target + ".xte-old"], b"precious")
        self.assertEqual(card.uploads, 0)

    def test_wrong_local_manifest_is_not_considered_matching(self):
        self.assertFalse(same_file({"exists": True, "dir": False, "size": 8, "sha256": "0" * 64}, self.expected))

    def test_bundle_rejects_corruption_and_path_traversal(self):
        data = {"schema": 1, "firmware": {"file": "app.bin", "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}, "assets": [], "support": []}
        (self.root / "app.bin").write_bytes(b"x")
        (self.root / "manifest.json").write_text(json.dumps(data))
        Bundle(self.root)
        (self.root / "app.bin").write_bytes(b"y")
        with self.assertRaisesRegex(ValueError, "损坏"):
            Bundle(self.root)
        for name in ("../app.bin", "/app.bin", "C:/app.bin", "..\\app.bin"):
            data["firmware"]["file"] = name
            (self.root / "manifest.json").write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Bundle(self.root)

    def test_layout_verifies_ota_and_rejects_wrong_slot_size_or_bootloader(self):
        system, parser, profile = fixture_layout()
        _, recovery, target = inspect_layout(system, parser, [profile], 900000)
        self.assertEqual(target.offset, 0x650000)
        self.assertEqual(recovery.offset, 0x10000)
        for offset, value in ((0xf000, b"\xff" * 32), (0xe000, b"\x00" * 32), (10, b"x")):
            changed = bytearray(system)
            changed[offset:offset + len(value)] = value
            if offset == 0xe000:  # app1 is still a valid selected slot
                inspect_layout(bytes(changed), parser, [profile], 900000)
            else:
                with self.assertRaises(ValueError):
                    inspect_layout(bytes(changed), parser, [profile], 900000)
        with self.assertRaises(ValueError):
            inspect_layout(system, parser, [profile], 0x640001)
        pending = bytearray(system)
        pending[0xf018:0xf01c] = struct.pack("<I", 1)
        with self.assertRaisesRegex(ValueError, "待确认"):
            inspect_layout(bytes(pending), parser, [profile], 100)

    def test_flash_only_writes_app_and_reuses_backup_on_repeat(self):
        system, parser, profile = fixture_layout()
        memory = bytearray(b"\xff" * FLASH_SIZE)
        memory[:0x10000] = system
        profile["recovery_md5"] = hashlib.md5(memory[0x10000:0x650000]).hexdigest()
        shutil.copyfile(PARSER, self.root / "gen_esp32part.py")
        image = self.root / "app.bin"
        image.write_bytes(b"application" * 100)
        bundle = SimpleNamespace(path=self.root, file=lambda _: image,
                                 manifest={"firmware": {}, "devices": [profile]})
        esp = Mock(CHIP_NAME="ESP32-C3", secure_download_mode=False)
        esp.run_stub.return_value = esp
        esp.get_secure_boot_enabled.return_value = False
        esp.get_flash_encryption_enabled.return_value = False
        esp.read_mac.return_value = (1, 2, 3, 4, 5, 6)
        esp.read_flash.side_effect = lambda offset, size, *args: bytes(memory[offset:offset+size])
        esp.flash_md5sum.side_effect = lambda offset, size: hashlib.md5(memory[offset:offset+size]).hexdigest()
        writes = []
        def tool(argv, esp):
            self.assertIn("--no-stub", argv)  # Reuse the already running official stub.
            self.assertEqual(argv[argv.index("--port")+1], "COM3")
            if "write_flash" in argv:
                offset = int(argv[-2], 0)
                data = Path(argv[-1]).read_bytes()
                writes.append((offset, len(data)))
                memory[offset:offset+len(data)] = data
        with patch("esptool.detect_chip", return_value=esp), patch("esptool.cmds.detect_flash_size", return_value="16MB"), \
             patch("pc_driver.flash.validate_image"), patch("esptool.main", side_effect=tool):
            for _ in range(2):
                flash_device("COM3", bundle, self.root / "user", lambda *args: None)
        self.assertEqual(writes, [(0x650000, 1100), (0x650000, 1100)])
        self.assertEqual(bytes(memory[:0x10000]), system)
        full_reads = [call for call in esp.read_flash.call_args_list if call.args[:2] == (0, FLASH_SIZE)]
        self.assertEqual(len(full_reads), 1)
        self.assertEqual(len(list((self.root / "user/backups/010203040506").glob("current-full-*.bin"))), 1)

    def test_download_failure_precedes_device_access(self):
        bundle = SimpleNamespace(manifest={"version": "test"})
        exclusive = Mock()
        installer = Installer(self.root, "COM3", lambda *args: None, threading.Event(), exclusive, bundle=bundle)
        with patch("pc_driver.install.device_identity", return_value="010203040506"), \
             patch("pc_driver.install.prepare_fonts", side_effect=OSError("offline")), \
             patch("pc_driver.install.flash_device") as flash:
            with self.assertRaisesRegex(OSError, "offline"):
                installer.run("all")
            flash.assert_not_called()
            exclusive.assert_not_called()

    def test_official_download_hash_failure_discards_partial_archive(self):
        (self.root / "font-reference.json").write_text(json.dumps({"source_zip_bytes": 3,
            "files": [{"file": "missing", "bytes": 1, "sha256": "0" * 64}]}))
        with patch("pc_driver.install.urllib.request.urlopen", return_value=BytesIO(b"abc")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                prepare_fonts(self.root, SimpleNamespace(path=self.root), lambda *args: None, lambda: None)
        self.assertFalse((self.root / "cache/MiSans.zip").exists())
        self.assertFalse((self.root / "cache/MiSans.zip.part").exists())

    def test_valid_font_cache_does_not_access_network(self):
        path = self.root / "cache/misans-4.009-medium-v1/fonts/font.epdfont"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"font")
        expected = {"file": path.name, "bytes": 4, "sha256": hashlib.sha256(b"font").hexdigest()}
        (self.root / "font-reference.json").write_text(json.dumps({"files": [expected]}))
        with patch("pc_driver.install.urllib.request.urlopen") as download:
            self.assertEqual(prepare_fonts(self.root, SimpleNamespace(path=self.root), lambda *args: None, lambda: None), [(path, expected)])
            download.assert_not_called()

    def test_full_card_can_verify_and_skip_already_installed_resources(self):
        card = Card({self.target: b"new font"})
        request = card.request
        card.request = lambda op, **fields: {"free": 0, "cluster": 4096} if op == "space" else request(op, **fields)
        card.mount = lambda: None
        card.install_version = 1
        bundle = SimpleNamespace(manifest={"version": "test", "assets": [self.expected]}, file=lambda _: self.local)
        connection = Mock()
        connection.read.return_value = b"XTE: resources ready\r\n"
        installer = Installer(self.root, "COM3", lambda *args: None, threading.Event(), nullcontext, bundle=bundle)
        with patch("pc_driver.install.device_identity", return_value="010203040506"), \
             patch("pc_driver.install.wait_device", return_value=(connection, "COM3")), \
             patch("pc_driver.install.TfClient", return_value=card):
            installer.run("assets")
        self.assertEqual(card.uploads, 0)
        connection.close.assert_called_once()

    def test_exclusive_device_restores_driver_even_after_failure(self):
        with patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(self.root)
            with self.assertRaises(OSError):
                with app.exclusive_device():
                    self.assertTrue(app.maintenance.is_set())
                    self.assertTrue(app.session_stop.is_set())
                    raise OSError()
            self.assertFalse(app.maintenance.is_set())


if __name__ == "__main__":
    unittest.main()
