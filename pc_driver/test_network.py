import hashlib
import hmac
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pc_driver.driver import (AgentFeed, AgentManager, advance_deadline, demo_status, main)
from pc_driver.network import PacketCodec, Session, TcpConnection, load_config, save_config, wifi_config
from pc_driver.protocol import MAX_PAYLOAD_BYTES, encode_agent

DEVICE = "0123456789ab"
KEY = "ab" * 32
NONCE = "cd" * 16


class FakeUsb:
    def __init__(self, mode="usb"):
        self.mode = mode
        self.port = "COM3"
        self.pending = bytearray()
        self.frames = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.pending)

    def write(self, frame):
        self.frames.append(frame)
        value = json.loads(frame)
        if value.get("cmd") in ("hello", "network_commit"):
            self.pending.extend(b"XTE: link " + json.dumps({"device": DEVICE, "mode": self.mode,
                                "configured": True, "events": 1, "ok": True,
                                "id": value.get("id", 0)}).encode() + b"\n")
        return len(frame)

    def read(self, size):
        value = bytes(self.pending[:size])
        del self.pending[:size]
        return value

    def close(self):
        self.closed = True


class FakeTcp:
    in_waiting = 0
    def __init__(self, *args):
        self.frames = []
        self.closed = False

    def write(self, frame):
        self.frames.append(frame)
        return len(frame)

    def close(self):
        self.closed = True


class NetworkTests(unittest.TestCase):
    def codec(self, server=False):
        return PacketCodec(bytes.fromhex(KEY), NONCE, b"S" if server else b"C", b"C" if server else b"S")

    def test_packets_fragmented_and_coalesced_at_every_boundary(self):
        frames = [b'{"v":2,"cmd":"heartbeat"}\n', b"XTE: volume_delta +5 42\n"]
        sender = self.codec()
        wire = b"".join(sender.encode(frame) for frame in frames)
        for cut in range(len(wire) + 1):
            receiver = self.codec(True)
            self.assertEqual(receiver.feed(wire[:cut]) + receiver.feed(wire[cut:]), b"".join(frames))

    def test_hmac_has_fixed_protocol_vector(self):
        wire = self.codec().encode(b'{"v":2,"cmd":"heartbeat"}\n')
        # Independently assemble the wire header and authentication input.
        self.assertEqual(wire[:6], struct.pack("!IH", 1, len(wire) - 38))
        self.assertEqual(wire[-32:], hmac.new(bytes.fromhex(KEY), b"C:" + NONCE.encode() + wire[:-32], hashlib.sha256).digest())
        self.assertEqual(wire.hex(), "00000001001a7b2276223a322c22636d64223a22686561727462656174227d0a"
                                    "4e93e351778cfe58f6b25a81a35ba68ae7787969ebef4cf934a15ca5df6daac0")

    def test_tamper_replay_wrong_direction_nonce_and_key_rejected(self):
        wire = self.codec().encode(b"XTE: pong\n")
        damaged = bytearray(wire); damaged[8] ^= 1
        for receiver, packet in ((self.codec(True), damaged), (self.codec(), wire),
                                (PacketCodec(b"x" * 32, NONCE, b"S", b"C"), wire),
                                (PacketCodec(bytes.fromhex(KEY), "ee" * 16, b"S", b"C"), wire)):
            with self.assertRaises(OSError):
                receiver.feed(packet)
        receiver = self.codec(True)
        self.assertEqual(receiver.feed(wire), b"XTE: pong\n")
        with self.assertRaises(OSError):
            receiver.feed(wire)

    def test_packet_length_and_sequence_limits(self):
        for size in (0, MAX_PAYLOAD_BYTES + 2, 65535):
            with self.assertRaises(OSError):
                self.codec(True).feed(struct.pack("!IH", 1, size))
        codec = self.codec()
        codec.tx = 0xffffffff
        with self.assertRaises(OSError):
            codec.encode(b"x\n")
        with self.assertRaises(OSError):
            self.codec().encode(b"x" * (MAX_PAYLOAD_BYTES + 1) + b"\n")

    def test_real_tcp_handshake_and_fragmented_signed_messages(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        listener.settimeout(3)
        errors = []
        def serve():
            try:
                with listener.accept()[0] as peer:
                    peer.settimeout(3)
                    peer.sendall(json.dumps({"device": DEVICE, "nonce": NONCE}).encode() + b"\n")
                    auth = bytearray()
                    while not auth.endswith(b"\n"):
                        auth.extend(peer.recv(1))
                    expected = hmac.digest(bytes.fromhex(KEY), f"client:{DEVICE}:{NONCE}".encode(), "sha256").hex()
                    self.assertEqual(json.loads(auth)["auth"], expected)
                    proof = hmac.digest(bytes.fromhex(KEY), f"server:{DEVICE}:{NONCE}".encode(), "sha256").hex()
                    peer.sendall(json.dumps({"ok": True, "proof": proof}).encode() + b"\n")
                    decoder = self.codec(True)
                    got = b""
                    while not got:
                        got += decoder.feed(peer.recv(7))
                    self.assertEqual(got, b'{"v":2,"cmd":"heartbeat"}\n')
                    packet = decoder.encode(b"XTE: pong\n")
                    for offset in range(0, len(packet), 3):
                        peer.sendall(packet[offset:offset + 3])
            except Exception as exc:
                errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=serve)
        thread.start()
        connection = TcpConnection("127.0.0.1", DEVICE, KEY, listener.getsockname()[1])
        try:
            connection.write(b'{"v":2,"cmd":"heartbeat"}\n')
            data = b""
            deadline = time.monotonic() + 3
            while not data and time.monotonic() < deadline:
                if connection.in_waiting:
                    data += connection.read(4096)
                else:
                    time.sleep(0.005)
            self.assertEqual(data, b"XTE: pong\n")
        finally:
            connection.close(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_wrong_device_handshake_closes_socket(self):
        fake = Mock()
        fake.recv.side_effect = [bytes([b]) for b in json.dumps({"device": "different", "nonce": NONCE}).encode() + b"\n"]
        with patch("pc_driver.network.socket.create_connection", return_value=fake), self.assertRaises(OSError):
            TcpConnection("127.0.0.1", DEVICE, KEY)
        fake.close.assert_called_once()

    def test_config_upgrade_and_atomic_updates_preserve_other_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tray.json"
            path.write_text('{"port":"COM7"}')
            self.assertEqual(load_config(path)["mode"], "auto")
            save_config(path, interval=120, devices={DEVICE: KEY}, device=DEVICE)
            save_config(path, mode="lan")
            config = load_config(path)
            self.assertEqual((config["port"], config["interval"], config["devices"]), ("COM7", 120, {DEVICE: KEY}))
            self.assertFalse(path.with_suffix(".tmp").exists())
            path.write_text('{"interval":NaN,"mode":"bad","devices":[],"device":[]}')
            self.assertEqual(load_config(path)["interval"], 5)

    def test_wifi_utf8_bounds_and_open_network(self):
        self.assertEqual(json.loads(wifi_config(DEVICE, KEY, "中文WiFi", "secret123"))["ssid"], "中文WiFi")
        self.assertEqual(json.loads(wifi_config(DEVICE, KEY, "open", ""))["password"], "")
        for ssid, password in (("中" * 11, "secret123"), ("bad\0", "secret123"),
                               ("ok", "short"), ("ok", "x" * 64), ("ok", "密" * 8)):
            with self.assertRaises(ValueError):
                wifi_config(DEVICE, KEY, ssid, password)

    def test_auto_follows_manual_firmware_mode_and_has_one_active_transport(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.Discovery") as discovery, \
             patch("pc_driver.network.TcpConnection", side_effect=FakeTcp):
            path = Path(folder) / "tray.json"
            save_config(path, devices={DEVICE: KEY}, device=DEVICE)
            discovery.return_value.addresses.return_value = {DEVICE: "127.0.0.1"}
            usb = FakeUsb()
            session = Session(lambda port: usb, path, "auto", "COM3")
            session.tick(); self.assertIs(session.active, usb)
            usb.mode = "lan"; session.hello_at = 0
            session.tick(); tcp = session.tcp
            self.assertIs(session.active, tcp)
            session.write(b'{"v":2,"cmd":"heartbeat"}\n')
            self.assertEqual(len(tcp.frames), 1)
            self.assertFalse(any(b"heartbeat" in frame for frame in usb.frames))
            usb.mode = "usb"; session.hello_at = 0
            session.tick()
            self.assertIs(session.active, usb); self.assertTrue(tcp.closed)
            session.close(); self.assertTrue(usb.closed)

    def test_fixed_mode_does_not_override_firmware_or_connect_unpaired_lan(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.TcpConnection") as tcp:
            path = Path(folder) / "tray.json"
            usb = FakeUsb("usb")
            session = Session(lambda port: usb, path, "lan", "COM3", "127.0.0.1")
            session.tick(); self.assertIsNone(session.active); tcp.assert_not_called()
            session.close()
            usb = FakeUsb("lan")
            session = Session(lambda port: usb, path, "usb", "COM3")
            session.tick(); self.assertIsNone(session.active); tcp.assert_not_called()
            session.close()

    def test_plugging_usb_does_not_interrupt_lan_before_firmware_mode_reply(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.Discovery"):
            path = Path(folder) / "tray.json"
            save_config(path, devices={DEVICE: KEY}, device=DEVICE)
            usb = FakeUsb()
            usb.write = lambda frame: len(frame)  # USB has not replied yet.
            session = Session(lambda port: usb, path, "auto", "COM3")
            tcp = session.tcp = FakeTcp()
            session.last_rx = time.monotonic()
            session.tick()
            self.assertIs(session.active, tcp)
            self.assertFalse(tcp.closed)
            session.close()

    def test_late_capability_reply_restarts_event_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            usb = FakeUsb()
            usb.write = lambda frame: len(frame)
            session = Session(lambda port: usb, Path(folder) / "tray.json", "usb", "COM3")
            session.tick()
            previous = session.epoch
            usb.pending.extend(b'XTE: link ' + json.dumps({"device": DEVICE, "mode": "usb", "configured": True, "events": 1}).encode() + b'\n')
            usb.pending.extend(b"XTE: volume_delta +5 42\n")
            session.tick()
            self.assertGreater(session.epoch, previous)
            self.assertTrue(session.supports_events)
            self.assertIn(b"volume_delta +5 42", session.read(4096))
            session.close()

    def test_failed_upload_never_sends_commit_and_wrong_device_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.TfClient") as client:
            usb = FakeUsb()
            session = Session(lambda port: usb, Path(folder) / "tray.json", port="COM3")
            session.tick()
            client.return_value.upload_bytes.side_effect = OSError("card write failed")
            with self.assertRaises(OSError):
                session.provision(DEVICE, "test", "secret123")
            self.assertFalse(any(b"network_commit" in frame for frame in usb.frames))
            with self.assertRaises(OSError):
                session.provision("0" * 12, "test", "secret123")
            session.close()

    def test_provision_streams_utf8_and_commits_only_after_verified_upload(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.TfClient") as client:
            usb = FakeUsb()
            path = Path(folder) / "tray.json"
            session = Session(lambda port: usb, path, port="COM3")
            session.tick(); session.provision(DEVICE, "中文WiFi", "secret123")
            data = client.return_value.upload_bytes.call_args.args[0]
            self.assertEqual(json.loads(data)["key"], load_config(path)["devices"][DEVICE])
            self.assertEqual(json.loads(data)["ssid"], "中文WiFi")
            self.assertEqual(len([frame for frame in usb.frames if b"network_commit" in frame]), 1)
            self.assertNotIn("secret123", path.read_text())
            session.close()

    def test_picture_upload_uses_file_sender_and_cleans_temporary_picture(self):
        with tempfile.TemporaryDirectory() as folder, patch("pc_driver.network.TfClient") as client:
            usb = FakeUsb()
            session = Session(lambda port: usb, Path(folder) / "tray.json", port="COM3")
            session.tick()
            seen = []
            def upload(local, remote):
                seen.append((local, remote, local.read_bytes()))
            client.return_value.upload.side_effect = upload
            session.upload_sleep_images([("/.sleep/one.bmp", b"first"), ("/.sleep/two.bmp", b"second")])
            self.assertEqual([(remote, data) for _,remote,data in seen], [("/.sleep/one.bmp",b"first"),("/.sleep/two.bmp",b"second")])
            self.assertTrue(all(not local.exists() for local,_,_ in seen))
            client.return_value.upload_bytes.assert_not_called()
            session.close()

    def test_maximum_agent_event_stays_bounded(self):
        frame = encode_agent({"tool": "codex", "state": "ask", "id": "x" * 36, "text": "\\" * 48})
        self.assertLessEqual(len(frame) - 1, MAX_PAYLOAD_BYTES)

    def test_event_does_not_move_regular_deadline_and_missed_ticks_do_not_burst(self):
        deadline = advance_deadline(100, 100, 30)
        self.assertEqual(deadline, 130)
        self.assertEqual(advance_deadline(deadline, 110, 30), 130)
        self.assertEqual(advance_deadline(deadline, 199, 30), 220)

    def test_bridge_event_is_sent_while_sensor_collection_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agents.json"
            manager = AgentManager(status_path=path, action_path=Path(folder) / "actions.jsonl")
            blocked, release, delivered, stopped = (threading.Event() for _ in range(4))
            frames = []
            class Pipe:
                active = True; supports_events = True; epoch = 0; label = "test"; in_waiting = 0
                def tick(self): pass
                def write(self, frame):
                    value = json.loads(frame); frames.append(value)
                    if value.get("cmd") == "agents" and value["agents"][0].get("id") == "urgent":
                        delivered.set()
                    return len(frame)
                def close(self): pass
            def sensors():
                blocked.set(); release.wait(3); return demo_status()
            with patch("pc_driver.driver.AgentManager", return_value=manager), \
                 patch.object(manager, "_process_running", return_value=False), \
                 patch("pc_driver.driver.WindowsVolume", return_value=Mock(get_percent=lambda: 55)), \
                 patch("pc_driver.driver.collect_status", side_effect=sensors), \
                 patch("pc_driver.network.Session", return_value=Pipe()):
                thread = threading.Thread(target=main, args=(["--mode", "auto", "--interval", "3600"], stopped))
                thread.start()
                try:
                    self.assertTrue(blocked.wait(2))
                    path.write_text(json.dumps({"codex": {"state": "ask", "id": "urgent", "text": "Allow"}}))
                    self.assertTrue(delivered.wait(1))
                    self.assertFalse(any("host" in frame for frame in frames))
                    self.assertTrue(any(frame.get("cmd") == "heartbeat" for frame in frames))
                finally:
                    stopped.set(); release.set(); thread.join(4)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
