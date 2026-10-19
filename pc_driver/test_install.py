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
from unittest.mock import MagicMock, Mock, patch
import zlib
import struct

from pc_driver.flash import inspect_layout, partition_parser, flash_device, FLASH_SIZE
from pc_driver.install import Bundle, Installer, install_file, prepare_fonts, same_file, wait_device
from pc_driver.install_window import build_window
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

    def test_cancel_after_upload_preserves_original_and_verified_candidate(self):
        card = Card({self.target: b"old"})
        def cancel():
            if card.uploads:
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            self.install(card, cancel)
        self.assertEqual(card.files, {self.target: b"old", self.target + ".xte-next": b"new font"})
        self.install(card)
        self.assertEqual(card.uploads, 1)

    def test_cancel_during_final_rename_completes_transaction_before_stopping(self):
        card = Card({self.target: b"old"})
        request = card.request
        cancel = threading.Event()
        def switch(op, **fields):
            result = request(op, **fields)
            if op == "rename":
                cancel.set()
            return result
        card.request = switch
        def check():
            if cancel.is_set():
                raise InterruptedError()
        self.assertTrue(self.install(card, check))
        self.assertTrue(cancel.is_set())
        self.assertEqual(card.files, {self.target: b"new font"})

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

    def test_cancel_during_prewrite_backup_does_not_publish_or_flash(self):
        system, parser, profile = fixture_layout()
        profile["recovery_md5"] = hashlib.md5(b"\x00" * 0x640000).hexdigest()
        shutil.copyfile(PARSER, self.root / "gen_esp32part.py")
        image = self.root / "app.bin"
        image.write_bytes(b"application")
        bundle = SimpleNamespace(path=self.root, file=lambda _: image,
                                 manifest={"firmware": {}, "devices": [profile]})
        for stage in ("full", "app1"):
            with self.subTest(stage=stage):
                user = self.root / stage
                device = user / "backups/010203040506"
                device.mkdir(parents=True)
                if stage == "app1":
                    saved = device / "existing-full.bin"
                    with saved.open("wb") as output:
                        output.write(system)
                        output.seek(FLASH_SIZE - 1)
                        output.write(b"\x00")
                    (device / "full-flash.json").write_text(json.dumps({"device": "010203040506",
                        "path": str(saved), "sha256": hashlib.sha256(saved.read_bytes()).hexdigest()}))
                cancel = threading.Event()
                esp = Mock(CHIP_NAME="ESP32-C3", secure_download_mode=False)
                esp.run_stub.return_value = esp
                esp.get_secure_boot_enabled.return_value = False
                esp.get_flash_encryption_enabled.return_value = False
                esp.read_mac.return_value = (1, 2, 3, 4, 5, 6)
                esp.flash_md5sum.return_value = profile["recovery_md5"]
                def read(offset, size, *args):
                    if size == 0x10000:
                        return system
                    self.assertEqual((offset, size), (0, FLASH_SIZE) if stage == "full" else (0x650000, 0x640000))
                    cancel.set()
                    args[0](4096, size)
                    self.fail("backup cancellation must unwind the read")
                esp.read_flash.side_effect = read
                def check():
                    if cancel.is_set():
                        raise InterruptedError("stop backup")
                with patch("esptool.detect_chip", return_value=esp), \
                     patch("esptool.cmds.detect_flash_size", return_value="16MB"), \
                     patch("pc_driver.flash.validate_image"), patch("esptool.main") as write:
                    with self.assertRaisesRegex(InterruptedError, "stop backup"):
                        flash_device("COM3", bundle, user, Mock(), check_cancel=check)
                write.assert_not_called()
                self.assertFalse(list(device.glob("current-full-*.bin")))
                self.assertFalse(list(device.glob("app1-*.bin")))
                self.assertFalse(list(device.glob("update-*.json")))
                self.assertEqual((device / "full-flash.json").exists(), stage == "app1")
                esp._port.close.assert_called_once()

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

    def test_cancel_slow_download_does_not_publish_cache_or_touch_device(self):
        (self.root / "font-reference.json").write_text(json.dumps({"source_zip_bytes": 3,
            "files": [{"file": "missing", "bytes": 1, "sha256": "0" * 64}]}))
        cancel = threading.Event()
        response = MagicMock()
        response.__enter__.return_value = response
        def read(_):
            cancel.set()
            return b"abc"
        response.read1.side_effect = read
        def check():
            if cancel.is_set():
                raise InterruptedError()
        with patch("pc_driver.install.urllib.request.urlopen", return_value=response) as download:
            with self.assertRaises(InterruptedError):
                prepare_fonts(self.root, SimpleNamespace(path=self.root), lambda *args: None, check)
        self.assertEqual(download.call_args.kwargs["timeout"], 30)
        self.assertFalse((self.root / "cache/MiSans.zip").exists())
        self.assertFalse((self.root / "cache/MiSans.zip.part").exists())

    def test_download_total_deadline_discards_partial_archive(self):
        (self.root / "font-reference.json").write_text(json.dumps({"source_zip_bytes": 3,
            "files": [{"file": "missing", "bytes": 1, "sha256": "0" * 64}]}))
        with patch("pc_driver.install.urllib.request.urlopen", return_value=BytesIO(b"abc")), \
             patch("pc_driver.install.time.monotonic", side_effect=[0, 1, 901]):
            with self.assertRaisesRegex(TimeoutError, "15 分钟"):
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

    def test_wait_device_cancel_closes_open_port_without_retry(self):
        cancel = threading.Event()
        connection = Mock()
        connection.read.side_effect = lambda _: cancel.set() or b""
        port = SimpleNamespace(device="COM3", vid=0x303A, pid=0x1001)
        def check():
            if cancel.is_set():
                raise InterruptedError()
        with patch("serial.tools.list_ports.comports", return_value=[port]), \
             patch("pc_driver.driver.open_serial", return_value=connection) as opened, \
             patch("pc_driver.install.time.sleep"):
            with self.assertRaises(InterruptedError):
                wait_device("COM3", check_cancel=check)
        connection.close.assert_called_once()
        opened.assert_called_once()

    def test_cancel_waiting_for_driver_releases_maintenance_without_device_work(self):
        with patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(self.root)
        app.driver_idle = Mock()
        app.driver_idle.wait.return_value = False
        app.installer_cancel.set()
        with self.assertRaises(InterruptedError):
            with app.exclusive_device():
                self.fail("cancelled maintenance must not start")
        app.driver_idle.wait.assert_called_once_with(.1)
        self.assertFalse(app.maintenance.is_set())

    def test_wait_device_discovery_cancel_does_not_wait_entire_deadline(self):
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise InterruptedError()
        with patch("serial.tools.list_ports.comports", return_value=[]) as ports, \
             patch("pc_driver.install.time.sleep") as sleep:
            with self.assertRaises(InterruptedError):
                wait_device("COM3", check_cancel=check)
        ports.assert_called_once()
        sleep.assert_called_once_with(.2)

    def test_cancel_after_flash_validation_does_not_start_next_stage(self):
        cancel = threading.Event()
        bundle = SimpleNamespace(manifest={"version": "test"})
        installer = Installer(self.root, "COM3", Mock(), cancel, nullcontext, bundle=bundle)
        with patch("pc_driver.install.device_identity", return_value="010203040506"), \
             patch("pc_driver.install.flash_device", side_effect=lambda *args: cancel.set()), \
             patch("pc_driver.install.wait_device") as wait:
            with self.assertRaises(InterruptedError):
                installer.run("firmware")
        wait.assert_not_called()

    def test_failed_tf_stage_aborts_and_releases_serial_and_maintenance(self):
        connection = Mock()
        card = Mock(install_version=1)
        card.request.side_effect = [None, OSError("card disconnected"), None]
        bundle = SimpleNamespace(manifest={"version": "test", "assets": [self.expected]}, file=lambda _: self.local)
        with patch("pc_driver.tray.pystray.Icon"):
            app = TrayApp(self.root)
        installer = Installer(self.root, "COM3", Mock(), app.installer_cancel, app.exclusive_device, bundle=bundle)
        with patch("pc_driver.install.device_identity", return_value="010203040506"), \
             patch("pc_driver.install.wait_device", return_value=(connection, "COM3")), \
             patch("pc_driver.install.TfClient", return_value=card):
            with self.assertRaisesRegex(OSError, "card disconnected"):
                installer.run("assets")
        self.assertEqual(card.request.call_args.args, ("abort",))
        self.assertEqual(card.timeout, 3)
        self.assertFalse(app.maintenance.is_set())
        connection.close.assert_called_once()

    def test_write_confirmation_cancel_does_not_start_worker_or_maintenance(self):
        port = SimpleNamespace(device="COM3", description="X4", vid=0x303A, pid=0x1001,
                               serial_number="010203040506")
        for maintenance in (False, True):
            with self.subTest(maintenance=maintenance), patch("pc_driver.tray.pystray.Icon"):
                app = TrayApp(self.root)
                app.exclusive_device = Mock()
                window, tk, ttk, dialogs = (MagicMock() for _ in range(4))
                dialogs.askokcancel.return_value = False
                ttk.Combobox.return_value.get.return_value = "COM3 · X4"
                commands = {}
                def button(*args, **kwargs):
                    commands[kwargs["text"]] = kwargs["command"]
                    return MagicMock()
                ttk.Button.side_effect = button
                with patch("pc_driver.install_window.Bundle", return_value=SimpleNamespace(manifest={"version": "test"})), \
                     patch("serial.tools.list_ports.comports", return_value=[port]), \
                     patch("pc_driver.install_window.Installer") as installer, \
                     patch("pc_driver.install_window.threading.Thread") as thread:
                    build_window(app, window, tk, ttk, dialogs, maintenance=maintenance)
                    commands["一键完整安装／更新"]()
                    commands["刷入配套固件"]()
                if maintenance:
                    self.assertEqual(dialogs.askokcancel.call_count, 2)
                    prompt = dialogs.askokcancel.call_args.args[1]
                    self.assertIn("COM3", prompt)
                    self.assertIn("060504030201", prompt)
                    self.assertIn("app1", prompt)
                    self.assertIn("完整 Flash", prompt)
                else:
                    dialogs.askokcancel.assert_not_called()
                self.assertFalse(app.installer_running.is_set())
                app.exclusive_device.assert_not_called()
                installer.assert_not_called()
                thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
