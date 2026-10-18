import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pc_driver.tf import CHUNK, MAX_PAYLOAD, PREFIX, TfClient, main, remote_path


class ScriptedSerial:
    """Scripted device replies, including unrelated SDK logs and acknowledgements."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.pending = bytearray()

    def write(self, frame):
        assert len(frame) - 1 <= MAX_PAYLOAD
        assert frame.endswith(b"\n")
        request = json.loads(frame)
        self.requests.append(request)
        response = {"id": request["id"], "ok": True, **next(self.responses)}
        self.pending.extend(b"[SD] log\nXTE: accepted\n")
        self.pending.extend(PREFIX + b'{"id":-1,"ok":true}\n')
        self.pending.extend(PREFIX + json.dumps(response).encode() + b"\n")
        return len(frame)

    def read(self, count):
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result


class TfTests(unittest.TestCase):
    def test_upload_all_byte_values_and_partial_chunk(self):
        data = bytes(range(256)) + b"\x00\xffend"
        digest = hashlib.sha256(data).hexdigest()
        connection = ScriptedSerial([{}, {"offset": 128}, {"offset": 256},
                                     {"offset": len(data)}, {"sha256": digest}])
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "font.bin"
            local.write_bytes(data)
            self.assertEqual(TfClient(connection).upload(local, "/fonts/font.bin"), (len(data), digest))
        writes = [request for request in connection.requests if request["op"] == "write"]
        self.assertEqual([request["offset"] for request in writes], [0, 128, 256])
        self.assertEqual(b"".join(bytes.fromhex(request["data"]) for request in writes), data)

    def test_zero_length_upload_and_download(self):
        digest = hashlib.sha256(b"").hexdigest()
        connection = ScriptedSerial([{}, {"sha256": digest}, {"size": 0}, {"sha256": digest}])
        with tempfile.TemporaryDirectory() as directory:
            local, copy = Path(directory) / "empty", Path(directory) / "copy"
            local.touch()
            client = TfClient(connection)
            self.assertEqual(client.upload(local, "/empty"), (0, digest))
            self.assertEqual(client.download("/empty", copy), (0, digest))
            self.assertEqual(copy.read_bytes(), b"")
        self.assertEqual([request["op"] for request in connection.requests], ["put", "finish", "get", "finish"])

    def test_download_verified_binary(self):
        data = bytes(range(256)) + b"last"
        digest = hashlib.sha256(data).hexdigest()
        responses = [{"size": len(data)}]
        for offset in range(0, len(data), CHUNK):
            block = data[offset:offset + CHUNK]
            responses.append({"data": block.hex(), "offset": offset + len(block)})
        responses.append({"sha256": digest})
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "copy.bin"
            self.assertEqual(TfClient(ScriptedSerial(responses)).download("/asset.bin", copy), (len(data), digest))
            self.assertEqual(copy.read_bytes(), data)
            self.assertFalse(copy.with_name("copy.bin.xte-part").exists())

    def test_download_corruption_never_publishes_partial_file(self):
        for response in ({"data": "00", "offset": 2}, {"data": "zz", "offset": 1},
                         {"data": "", "offset": 1}):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                copy = Path(directory) / "copy"
                with self.assertRaises(OSError):
                    TfClient(ScriptedSerial([{"size": 1}, response])).download("/x", copy)
                self.assertEqual(list(Path(directory).iterdir()), [])
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "copy"
            with self.assertRaisesRegex(OSError, "SHA-256"):
                TfClient(ScriptedSerial([{"size": 0}, {"sha256": "0" * 64}])).download("/x", copy)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_local_files_and_temporary_files_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "copy"
            temporary = Path(directory) / "copy.xte-part"
            client = TfClient(ScriptedSerial([]))
            copy.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                client.download("/x", copy)
            self.assertEqual(copy.read_bytes(), b"original")
            copy.unlink()
            temporary.write_bytes(b"interrupted")
            with self.assertRaises(FileExistsError):
                client.download("/x", copy)
            self.assertEqual(temporary.read_bytes(), b"interrupted")
            self.assertEqual(client.connection.requests, [])

    def test_card_and_overwrite_errors_surface(self):
        for error in ("no_card", "exists", "write_failed", "hash_mismatch"):
            with self.subTest(error=error), self.assertRaisesRegex(OSError, error):
                TfClient(ScriptedSerial([{"ok": False, "error": error}])).request("mount")

    def test_paths_and_maximum_request_frame(self):
        for path in ("relative", "/../x", "/./x", "/a//b", "/a/", "/a\\b", "/a:b", "/" + "x" * 96):
            with self.subTest(path=path), self.assertRaises(ValueError):
                remote_path(path)
        self.assertEqual(remote_path("/"), "/")
        self.assertEqual(remote_path("/x.xte-part"), "/x.xte-part")
        self.assertEqual(remote_path("/字库"), "/字库")
        font_path = "/fonts/Zheng20号.epdfont"
        font_connection = ScriptedSerial([{}])
        TfClient(font_connection).request("get", path=remote_path(font_path))
        self.assertEqual(font_connection.requests[0]["path"], font_path)
        connection = ScriptedSerial([{}])
        client = TfClient(connection)
        client.sequence = 0xfffffffe
        client.request("write", offset=0xffffffff, data="ff" * CHUNK)
        self.assertEqual(connection.requests[0]["id"], 0xffffffff)
        with self.assertRaises(ValueError):
            client.request("write", data="ff" * (CHUNK + 64))

    def test_list_and_mount(self):
        connection = ScriptedSerial([{"ready": True, "chunk": CHUNK}, {},
                                     {"name": "fonts", "dir": True, "size": 0},
                                     {"name": "x.bin", "dir": False, "size": 12}, {"eof": True}])
        client = TfClient(connection)
        client.mount()
        self.assertEqual([entry["name"] for entry in client.list()], ["fonts", "x.bin"])

    def test_negotiated_ymodem_requires_sd_readback_hash(self):
        data = bytes(range(256)) + b"partial"
        digest = hashlib.sha256(data).hexdigest()
        for sd_digest in (digest, None, "0" * 64):
            with self.subTest(sd_digest=sd_digest), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "binary.bin"
                path.write_bytes(data)
                connection = ScriptedSerial([{"ready": True, "chunk": CHUNK, "upload": "ymodem1k"},
                    {"transport": "ymodem1k"}, {"sha256": digest, "sd_sha256": sd_digest}])
                client = TfClient(connection)
                client.mount()
                with patch.object(client, "_send_ymodem") as send:
                    if sd_digest == digest:
                        self.assertEqual(client.upload(path, "/binary.bin"), (len(data), digest))
                    else:
                        with self.assertRaisesRegex(OSError, "SD read-back"):
                            client.upload(path, "/binary.bin")
                    send.assert_called_once_with(path)
                self.assertEqual(connection.requests[1]["transport"], "ymodem1k")

    def test_legacy_fallback_and_config_never_start_binary_mode(self):
        data = b"config"
        digest = hashlib.sha256(data).hexdigest()
        for prefer, in_memory in ((False, False), (True, True)):
            with self.subTest(prefer=prefer), tempfile.TemporaryDirectory() as directory:
                connection = ScriptedSerial([{"ready": True, "chunk": CHUNK, "upload": "ymodem1k"},
                    {}, {"offset": len(data)}, {"sha256": digest}])
                client = TfClient(connection, prefer_ymodem=prefer)
                client.mount()
                path = Path(directory) / "config"; path.write_bytes(data)
                with patch.object(client, "_send_ymodem") as send:
                    result = client.upload_bytes(data, "/config") if in_memory else client.upload(path, "/config")
                    self.assertEqual(result, (len(data), digest))
                    send.assert_not_called()
                self.assertNotIn("transport", connection.requests[1])

    def test_bulk_read_preserves_next_response_bytes(self):
        connection = Mock()
        connection.in_waiting = 8
        connection.read.return_value = b"\x06CXTE: t"
        client = TfClient(connection)
        self.assertEqual(client._read(1), b"\x06")
        self.assertEqual(client._read(1), b"C")
        self.assertEqual(client._read(6), b"XTE: t")
        connection.read.assert_called_once_with(8)

    def test_partial_usb_reply_wakes_with_read_only_hello_without_replaying_write(self):
        class StalledSerial:
            def __init__(self):
                self.pending = bytearray()
                self.tail = b""
                self.operations = []
            @property
            def in_waiting(self): return len(self.pending)
            def write(self, frame):
                request = json.loads(frame)
                self.operations.append(request)
                if request["cmd"] == "hello":
                    self.pending.extend(self.tail + b'XTE: link {"ok":true}\n')
                    self.tail = b""
                else:
                    reply = PREFIX + json.dumps({"id":request["id"], "ok":True, "offset":128, "padding":"x"*200}).encode() + b"\n"
                    self.pending.extend(reply[:128]); self.tail = reply[128:]
                return len(frame)
            def read(self, count):
                data = bytes(self.pending[:count]); del self.pending[:count]; return data
        connection = StalledSerial()
        result = TfClient(connection, timeout=2).request("write", offset=0, data="00"*128)
        self.assertEqual(result["offset"],128)
        self.assertEqual([row["cmd"] for row in connection.operations], ["tf", "hello"])
        self.assertEqual([row.get("op") for row in connection.operations], ["write", None])

    def test_timeout_and_incomplete_serial_write(self):
        connection = Mock()
        connection.write.side_effect = lambda data: len(data)
        client = TfClient(connection, timeout=0)
        with self.assertRaises(TimeoutError):
            client.request("mount")
        connection.write.return_value = 0
        connection.write.side_effect = None
        with self.assertRaisesRegex(OSError, "Incomplete"):
            client.request("mount")

    def test_cli_closes_port_and_aborts_failed_operation(self):
        connection = ScriptedSerial([{"ready": True, "chunk": CHUNK},
                                     {"ok": False, "error": "not_found"}, {}])
        connection.open = Mock()
        connection.close = Mock()
        connection.is_open = True
        with patch("pc_driver.tf.serial.serial_for_url", return_value=connection), patch("sys.stderr"):
            self.assertEqual(main(["--port", "COM3", "ls", "/missing"]), 1)
        self.assertFalse(connection.dtr)
        self.assertFalse(connection.rts)
        self.assertEqual(connection.requests[-1]["op"], "abort")
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
