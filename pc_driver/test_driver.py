import json
import base64
import io
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.response
from unittest.mock import Mock, patch

from pc_driver.amd_gpu import AmdTelemetry, AdapterInfo, PMLog, select_gpu_reading
import ctypes

from pc_driver.driver import (AgentManager, DshApprovalAdapter, WindowsVolume, _WebSocket,
                              agent_confirmation, collect_status,
                              demo_status, open_serial, volume_confirmation)
from pc_driver.driver import main
from pc_driver.protocol import MAX_PAYLOAD_BYTES, METRICS, encode_status


class DriverTests(unittest.TestCase):
    def test_demo_round_trip_and_frame_boundary(self):
        frame = encode_status(demo_status())
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertLessEqual(len(frame) - 1, MAX_PAYLOAD_BYTES)
        expected = dict(demo_status())
        expected["cpu_temp_c"] = None
        self.assertEqual(json.loads(frame), expected)

    def test_serial_loopback(self):
        frame = encode_status(demo_status())
        with open_serial("loop://") as connection:
            self.assertFalse(connection.dtr)
            self.assertFalse(connection.rts)
            self.assertEqual(connection.write(frame), len(frame))
            self.assertEqual(connection.read(len(frame)), frame)

    def test_missing_metrics_are_null(self):
        sample = demo_status()
        for key in METRICS:
            sample[key] = None
            expected = dict(sample)
            expected["cpu_temp_c"] = None
            self.assertEqual(json.loads(encode_status(sample)), expected)

    def test_invalid_metrics_are_rejected(self):
        for value in (True, "12", -1, 101, float("nan"), float("inf")):
            with self.subTest(value=value):
                sample = demo_status()
                sample["cpu"] = value
                with self.assertRaises(ValueError):
                    encode_status(sample)

    def test_temperature_and_gpu_limits(self):
        for key, value in (("gpu_temp_c", -51), ("gpu_temp_c", 151), ("gpu", 101)):
            with self.subTest(key=key, value=value):
                sample = demo_status()
                sample[key] = value
                with self.assertRaises(ValueError):
                    encode_status(sample)

    def test_invalid_memory_is_rejected(self):
        sample = demo_status()
        sample["mem_total_mib"] = 1
        with self.assertRaises(ValueError):
            encode_status(sample)

    def test_invalid_labels_are_rejected(self):
        for key, value in (("host", "A" * 33), ("host", "A\nB"), ("host", "\u4e2d"),
                           ("time", "x" * 19), ("v", True)):
            with self.subTest(key=key, value=value):
                sample = demo_status()
                sample[key] = value
                with self.assertRaises(ValueError):
                    encode_status(sample)

    def test_live_collection_matches_schema(self):
        sample = collect_status()
        expected = dict(sample)
        expected["cpu_temp_c"] = None
        self.assertEqual(json.loads(encode_status(sample)), expected)
        self.assertGreater(sample["mem_total_mib"], 0)

    def test_volume_limits(self):
        for value in (-1, 101, True, "50"):
            with self.subTest(value=value):
                sample = demo_status()
                sample["volume"] = value
                with self.assertRaises(ValueError):
                    encode_status(sample)

    def test_volume_confirmation_uses_real_readback_and_preserves_metrics(self):
        endpoint = Mock()
        endpoint.GetMasterVolumeLevelScalar.side_effect = [0.97, 0.99]
        volume = object.__new__(WindowsVolume)
        volume._ok = True
        volume._endpoint = endpoint
        sample = demo_status()
        confirmed = volume_confirmation("XTE: volume_delta +5 42\r", volume, sample)
        endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(1.0, None)
        self.assertEqual(confirmed["volume"], 99.0)
        self.assertEqual(confirmed["volume_ack"], 42)
        self.assertEqual(confirmed["time"], sample["time"])
        self.assertEqual(sample["volume"], 55.0)
        self.assertEqual(json.loads(encode_status(confirmed))["volume_ack"], 42)

    def test_volume_reacquires_invalidated_audio_endpoint(self):
        from pc_driver.driver import COMError
        stale, fresh = Mock(), Mock()
        stale.GetMasterVolumeLevelScalar.side_effect = COMError(-2004287484, None, None)
        fresh.GetMasterVolumeLevelScalar.return_value = 0.4
        volume = object.__new__(WindowsVolume)
        volume._ok, volume._endpoint = True, stale
        def reconnect():
            volume._ok, volume._endpoint = True, fresh
        volume._connect = reconnect
        self.assertEqual(volume.get_percent(), 40.0)
        fresh.GetMasterVolumeLevelScalar.side_effect = COMError(-2004287484, None, None)
        volume._connect = lambda: setattr(volume, "_ok", False)
        self.assertIsNone(volume.get_percent())

    def test_volume_rejects_bad_commands_and_supports_legacy(self):
        volume = Mock()
        for line in ("XTE: accepted", "XTE: volume_delta +100 1", "XTE: volume_delta 0 1",
                     "XTE: volume_delta +2 1", "XTE: volume_delta +5 -1",
                     "XTE: volume_delta +5 4294967296", "XTE: volume_delta +5 x",
                     "XTE: volume_delta +5 1 extra"):
            self.assertIsNone(volume_confirmation(line, volume, demo_status()))
        volume.adjust.assert_not_called()
        volume.adjust.return_value = None
        confirmed = volume_confirmation("XTE: volume_delta -5", volume, demo_status())
        self.assertIsNone(confirmed["volume"])
        self.assertEqual(confirmed["volume_ack"], 0)

    def test_all_settings_volume_steps_are_confirmed(self):
        for delta in (-10, -5, -1, 1, 5, 10):
            with self.subTest(delta=delta):
                volume = Mock()
                volume.adjust.return_value = 73.0
                sample = demo_status()
                confirmed = volume_confirmation(f"XTE: volume_delta {delta:+d} 42", volume, sample)
                volume.adjust.assert_called_once_with(delta)
                self.assertEqual(confirmed["volume"], 73.0)
                self.assertEqual(confirmed["volume_ack"], 42)
                self.assertEqual(confirmed["time"], sample["time"])

    def test_volume_ack_limits(self):
        for value in (-1, 4294967296, True, "1", 1.5):
            sample = demo_status()
            sample["volume_ack"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode_status(sample)

    def test_duplicate_volume_sequence_acknowledges_without_second_adjustment(self):
        seen = {}
        volume = Mock()
        volume.adjust.return_value = 60
        first = volume_confirmation("XTE: volume_delta +5 42", volume, demo_status(), seen)
        repeat = volume_confirmation("XTE: volume_delta +5 42", volume, demo_status(), seen)
        self.assertEqual((first["volume"], repeat["volume"], repeat["volume_ack"]), (60, 60, 42))
        volume.adjust.assert_called_once_with(5)
        self.assertIsNone(volume_confirmation("XTE: volume_delta -5 42", volume, demo_status(), seen))

    def test_split_volume_command_is_confirmed_before_next_sample(self):
        class Connection:
            chunks = [b"XTE: vol", b"ume_delta +5 7\n"]
            frames = []
            closed = False

            @property
            def in_waiting(self):
                return len(self.chunks[0]) if self.frames and self.chunks else 0

            def read(self, size):
                return self.chunks.pop(0)

            def write(self, frame):
                value = json.loads(frame)
                if "host" in value:
                    self.frames.append(value)
                return len(frame)

            def close(self):
                self.closed = True

        connection = Connection()
        volume = Mock()
        volume.adjust.return_value = 60.0

        def sleep(_):
            if len(connection.frames) == 2:
                raise KeyboardInterrupt

        with patch("pc_driver.driver.open_serial", return_value=connection), \
             patch("pc_driver.driver.WindowsVolume", return_value=volume), \
             patch("pc_driver.driver.AgentManager") as agents, \
             patch("pc_driver.driver.collect_status", return_value=demo_status()) as collect, \
             patch("pc_driver.driver.time.monotonic", return_value=100.0), \
             patch("pc_driver.driver.time.sleep", side_effect=sleep), \
             patch("pc_driver.driver.psutil.cpu_percent"), \
             patch("pc_driver.driver.sys.stderr"):
            agents.return_value.collect.return_value = demo_status()["agents"]
            agents.return_value._handled = set()
            self.assertEqual(main(["--port", "COM3", "--once"]), 0)
        collect.assert_called_once()
        volume.adjust.assert_called_once_with(5)
        self.assertEqual(connection.frames[-1]["volume"], 60.0)
        self.assertEqual(connection.frames[-1]["volume_ack"], 7)
        self.assertTrue(connection.closed)

    def test_collection_failure_becomes_null(self):
        with patch("pc_driver.driver.psutil.virtual_memory", side_effect=OSError("unavailable")):
            sample = collect_status()
        self.assertIsNone(sample["mem_total_mib"])
        self.assertIsNone(json.loads(encode_status(sample))["mem_total_mib"])


    def test_amd_gpu_identity_and_ambiguity(self):
        readings = [((3, 0, 0), 16.0, 51.0), ((3, 0, 0), 16.0, 51.0),
                    ((15, 0, 0), 0.0, None)]
        self.assertEqual(select_gpu_reading(readings), (16.0, 51.0))
        self.assertEqual(select_gpu_reading(readings + [((4, 0, 0), 80.0, 60.0)]),
                         (None, None))

    def test_amd_unsupported_sensor_and_failed_query(self):
        reader = object.__new__(AmdTelemetry)
        reader.context = ctypes.c_void_p(1)
        reader.adapters = (AdapterInfo * 1)()
        reader.adapters[0].present = 1
        class FakeAdl:
            returncode = 0
            supported = 0
            def ADL2_New_QueryPMLogData_Get(self, context, index, pointer):
                log = ctypes.cast(pointer, ctypes.POINTER(PMLog)).contents
                log.sensors[8].supported = self.supported
                log.sensors[8].value = 51
                log.sensors[19].supported = self.supported
                log.sensors[19].value = 16
                return self.returncode
        reader.adl = FakeAdl()
        self.assertEqual(reader.read(), (None, None))
        reader.adl.supported = 1
        self.assertEqual(reader.read(), (16.0, 51.0))
        reader.adl.returncode = -8
        self.assertEqual(reader.read(), (None, None))

    def test_amd_metrics_reach_frame(self):
        with patch("pc_driver.driver.collect_amd_gpu", return_value=(16.0, 51.0)):
            sample = collect_status()
        frame = json.loads(encode_status(sample))
        self.assertEqual((frame["gpu"], frame["gpu_temp_c"]), (16.0, 51.0))

    def test_temperature_negative_and_null(self):
        sample = demo_status()
        sample["gpu_temp_c"] = -10
        self.assertEqual(json.loads(encode_status(sample))["gpu_temp_c"], -10)
        sample["gpu_temp_c"] = None
        self.assertIsNone(json.loads(encode_status(sample))["gpu_temp_c"])

    def test_agents_round_trip_and_frame_boundary(self):
        sample = demo_status()
        sample["agents"][0] = {"tool": "dsh", "state": "ask", "id": "request-1",
                               "text": "Allow command"}
        frame = encode_status(sample)
        self.assertLessEqual(len(frame) - 1, MAX_PAYLOAD_BYTES)
        self.assertEqual(json.loads(frame)["agents"], sample["agents"])

    def test_agent_schema_rejects_unknown_or_unbound_requests(self):
        for entry in ({"tool": "other", "state": "idle", "id": "", "text": ""},
                      {"tool": "dsh", "state": "ask", "id": "", "text": "pending"}):
            sample = demo_status()
            sample["agents"] = [entry]
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                encode_status(sample)

    def test_agent_action_requires_matching_request(self):
        with tempfile.TemporaryDirectory() as root:
            manager = AgentManager(status_path=f"{root}/agents.json", action_path=f"{root}/actions.jsonl")
            manager._overrides["dsh"] = {"tool": "dsh", "state": "ask", "id": "request-1", "text": "Allow"}
            with patch.object(manager, "_desktop_key", return_value=True):
                confirmed = agent_confirmation("XTE: agent_action approve dsh request-1", manager, demo_status())
        self.assertEqual(confirmed["agents"][0]["state"], "done")
        self.assertIsNone(agent_confirmation("XTE: agent_action approve dsh stale", manager, demo_status()))

    def test_live_dsh_request_replaces_previous_action_state(self):
        class FakeDsh:
            def __init__(self):
                self.pending = {"tool": "dsh", "state": "ask", "id": "old", "text": "old request"}

            def snapshot(self):
                return dict(self.pending) if self.pending else None

            def decide(self, request_id, action):
                if self.pending and self.pending["id"] == request_id and action == "approve":
                    self.pending = None
                    return True
                return False

        with tempfile.TemporaryDirectory() as root:
            live = FakeDsh()
            manager = AgentManager(status_path=f"{root}/agents.json", action_path=f"{root}/actions.jsonl",
                                   dsh_adapter=live)
            self.assertEqual(manager.collect()[0]["id"], "old")
            self.assertIsNotNone(agent_confirmation("XTE: agent_action approve dsh old", manager, demo_status()))
            live.pending = {"tool": "dsh", "state": "ask", "id": "new", "text": "new request"}
            self.assertEqual(manager.collect()[0]["id"], "new")


class DshApprovalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = self.root / "fake-credentials.yaml"
        secret = base64.urlsafe_b64encode(b"test-only-secret".ljust(32, b"x")).decode().rstrip("=")
        self.credentials.write_text(f"secret: {secret}\n", encoding="utf-8")
        for name, value in (("pc_driver.driver.threading.Thread", None),
                            ("pc_driver.driver.AgentManager._process_running", False)):
            target = patch(name, return_value=value) if value is not None else patch(name)
            result = target.start()
            self.addCleanup(target.stop)
            if value is None:
                self.thread = result

    def adapter(self, host="127.0.0.1"):
        adapter = DshApprovalAdapter(host=host, credentials_path=self.credentials)
        adapter._pending = {"event_id": "request-1", "client_id": "client-1", "text": "Allow"}
        return adapter

    def manager(self, adapter):
        return AgentManager(status_path=self.root / "agents.json",
                            action_path=self.root / "actions.jsonl", dsh_adapter=adapter)

    @staticmethod
    def response(status=200, headers=None):
        response = urllib.response.addinfourl(io.BytesIO(), headers or {}, "http://127.0.0.1:3080/", status)
        response.msg = "Test response"
        return response

    def assert_failed_without_keyboard(self, manager, request_id="request-1"):
        with patch.object(manager, "_desktop_key") as keyboard, \
             patch("pc_driver.driver.sys.stderr", new_callable=io.StringIO) as log:
            confirmed = agent_confirmation(f"XTE: agent_action approve dsh {request_id}", manager, demo_status())
        self.assertIsNone(confirmed)
        keyboard.assert_not_called()
        self.assertFalse(manager.action_path.exists())
        self.assertEqual(manager._handled, set())
        self.assertEqual(manager._overrides, {})
        self.assertNotIn("accepted", log.getvalue())
        return log.getvalue()

    def test_post_failure_keeps_request_without_keys_or_success_log(self):
        adapter = self.adapter()
        with patch("urllib.request.HTTPHandler.http_open", return_value=self.response(503)) as send:
            log = self.assert_failed_without_keyboard(self.manager(adapter))
        send.assert_called_once()
        self.assertIn("request-1", log)
        self.assertIn("approve", log)
        self.assertIn("HTTP 503", log)
        self.assertEqual(adapter.snapshot()["id"], "request-1")

    def test_live_adapter_rejection_never_uses_bridge_keyboard(self):
        adapter = self.adapter()
        with patch.object(adapter, "decide", return_value=False) as decide:
            self.assert_failed_without_keyboard(self.manager(adapter))
        decide.assert_called_once_with("request-1", "approve")

    def test_live_adapter_error_does_not_escape_into_keyboard_or_success(self):
        adapter = self.adapter()
        with patch.object(adapter, "decide", side_effect=OSError("Cookie=DO-NOT-LOG")):
            log = self.assert_failed_without_keyboard(self.manager(adapter))
        self.assertIn("OSError", log)
        self.assertNotIn("DO-NOT-LOG", log)

    def test_explicit_bridge_without_live_adapter_keeps_guarded_keyboard_compatibility(self):
        manager = self.manager(None)
        manager.status_path.write_text(json.dumps({"dsh": {"state": "ask", "id": "request-1", "text": "Allow"}}),
                                       encoding="utf-8")
        with patch.object(manager, "_desktop_key", return_value=True) as keyboard:
            confirmed = agent_confirmation("XTE: agent_action approve dsh request-1", manager, demo_status())
            self.assertIsNotNone(confirmed)
            self.assertEqual(confirmed["agents"][0]["state"], "done")
            self.assertFalse(manager.action("dsh", "request-1", "approve"))
        keyboard.assert_called_once_with("dsh", "approve")

    def test_stale_request_never_reaches_adapter_or_keyboard(self):
        adapter = self.adapter()
        with patch.object(adapter, "decide") as decide:
            self.assert_failed_without_keyboard(self.manager(adapter), "old-request")
        decide.assert_not_called()

    def test_success_and_duplicate_submit_once(self):
        adapter = self.adapter()
        manager = self.manager(adapter)
        with patch("urllib.request.HTTPHandler.http_open", return_value=self.response()) as send, \
             patch.object(manager, "_desktop_key") as keyboard:
            self.assertTrue(manager.action("dsh", "request-1", "approve"))
            self.assertFalse(manager.action("dsh", "request-1", "approve"))
            self.assertFalse(manager.action("dsh", "request-1", "reject"))
        send.assert_called_once()
        keyboard.assert_not_called()
        self.assertIsNone(adapter.snapshot())
        records = [json.loads(line) for line in manager.action_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(item["id"], item["action"]) for item in records], [("request-1", "approve")])

    def test_cancelled_request_cannot_be_accepted(self):
        adapter = self.adapter()
        adapter._consume({"type": "item", "value": {"type": "cancel", "eventId": "request-1"}})
        with patch.object(adapter, "_post_result") as send:
            self.assert_failed_without_keyboard(self.manager(adapter))
        send.assert_not_called()

    def test_cancel_during_post_cannot_log_old_request_as_accepted(self):
        adapter = self.adapter()
        def cancel(*_):
            adapter._consume({"type": "item", "value": {"type": "cancel", "eventId": "request-1"}})
        with patch.object(adapter, "_post_result", side_effect=cancel):
            log = self.assert_failed_without_keyboard(self.manager(adapter))
        self.assertIn("request ended or changed", log)

    def test_disconnect_discards_stream_request(self):
        adapter = self.adapter()
        websocket = Mock()
        websocket.recv.side_effect = [
            {"type": "item", "value": {"type": "ready", "clientId": "client-1"}},
            {"type": "item", "value": {"type": "waterfall", "event": "approval/request",
                                        "eventId": "request-1", "request": {"toolName": "test"}}},
            OSError("test disconnect"),
        ]
        with patch("pc_driver.driver._WebSocket", return_value=websocket), \
             patch.object(adapter._stop, "wait", side_effect=lambda _: adapter._stop.set()):
            adapter._run()
        websocket.close.assert_called_once()
        self.assertIsNone(adapter.snapshot())
        self.assertEqual(adapter._client_id, "")
        self.assert_failed_without_keyboard(self.manager(adapter))

    def test_remote_target_is_rejected_before_credentials_or_thread(self):
        for host in ("example.com", "192.168.1.2", "0.0.0.0", "::", "http://127.0.0.1", "127.0.0.1@evil"):
            with self.subTest(host=host), patch.object(Path, "read_text") as read, \
                 patch("pc_driver.driver.socket.create_connection") as connect:
                with self.assertRaisesRegex(ValueError, "loopback"):
                    self.adapter(host)
                read.assert_not_called()
                connect.assert_not_called()
        self.thread.assert_not_called()

    def test_environment_remote_target_is_rejected(self):
        with patch.dict("os.environ", {"XTE_DSH_HOST": "192.168.1.2"}), \
             patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(ValueError, "loopback"):
                DshApprovalAdapter(credentials_path=self.credentials)
        read.assert_not_called()
        self.thread.assert_not_called()

    def test_target_is_checked_again_before_credentials_or_post(self):
        adapter = self.adapter()
        adapter.host = "192.168.1.2"
        with patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(ValueError, "loopback"):
                adapter._cookie()
        read.assert_not_called()
        with patch.object(adapter, "_cookie") as cookie:
            with self.assertRaisesRegex(ValueError, "loopback"):
                adapter._post_result("client-1", "request-1", "allowed-once")
        cookie.assert_not_called()

    def test_loopback_post_and_cookie_authority_remain_compatible(self):
        for host, authority in (("localhost", "127.0.0.1:3080"), ("127.0.0.1", "127.0.0.1:3080"),
                                ("::1", "[::1]:3080")):
            with self.subTest(host=host):
                adapter = self.adapter(host)
                cookie = adapter._cookie()
                body = cookie.split("=v1.", 1)[1].split(".")[0]
                claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                self.assertEqual(claims["authority"], authority)
                with patch("urllib.request.HTTPHandler.http_open", return_value=self.response()) as send, \
                     patch.object(adapter, "_cookie", return_value=cookie):
                    self.assertTrue(adapter.decide("request-1", "reject"))
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, f"http://{authority}/api/$events/result")
                self.assertEqual(request.get_header("Cookie"), cookie)
                self.assertEqual(json.loads(request.data)["payload"]["args"]["outcome"]["value"], "rejected")

    def test_ipv6_websocket_host_header_uses_brackets(self):
        sock = Mock()
        sock.recv.return_value = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
        with patch("pc_driver.driver.socket.create_connection", return_value=sock) as connect:
            websocket = _WebSocket("::1", 3080, "fake-cookie")
        connect.assert_called_once_with(("::1", 3080), timeout=2.0)
        self.assertIn(b"Host: [::1]:3080\r\n", sock.sendall.call_args.args[0])
        websocket.close()

    def test_cookie_post_never_follows_redirect(self):
        for status in (301, 302, 303, 307, 308):
            for target in ("http://remote.example/result", "http://127.0.0.1:9999/result"):
                with self.subTest(status=status, target=target):
                    adapter = self.adapter()
                    with patch("urllib.request.HTTPHandler.http_open",
                               return_value=self.response(status, {"Location": target})) as send:
                        log = self.assert_failed_without_keyboard(self.manager(adapter))
                    send.assert_called_once()
                    self.assertIn(f"HTTP {status}", log)
                    self.assertNotIn(target, log)

    def test_failure_log_does_not_echo_exception_secrets(self):
        adapter = self.adapter()
        with patch.object(adapter, "_post_result", side_effect=urllib.error.URLError("Cookie=DO-NOT-LOG")):
            log = self.assert_failed_without_keyboard(self.manager(adapter))
        self.assertIn("URLError", log)
        self.assertNotIn("DO-NOT-LOG", log)


if __name__ == "__main__":
    unittest.main()
