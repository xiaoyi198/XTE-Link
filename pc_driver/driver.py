"""Collect PC metrics and send bounded JSON lines to an XTE-Link device."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sys
import time
import subprocess
import ctypes
import base64
import hashlib
import hmac
import re
import struct
import threading
import urllib.error
import urllib.request
import uuid

import psutil
import serial
from serial.tools import list_ports

if __package__:
    from .protocol import encode_status
    from .amd_gpu import collect_amd_gpu
else:
    from protocol import encode_status
    from amd_gpu import collect_amd_gpu


class AgentManager:
    """Bridge desktop approval state and guarded keyboard actions."""

    TOOLS = ("dsh", "codex")
    STATES = ("offline", "idle", "running", "ask", "done", "error")

    def __init__(self, status_path=None, action_path=None, dsh_adapter=None):
        default_root = Path(os.environ.get("USERPROFILE", Path.home())) / ".xte-link"
        self.status_path = Path(status_path or os.environ.get("XTE_AGENT_STATUS", default_root / "agents.json"))
        self.action_path = Path(action_path or os.environ.get("XTE_AGENT_ACTIONS", default_root / "actions.jsonl"))
        self._overrides = {}
        # Explicit test/bridge paths stay deterministic; the live adapter is
        # enabled for the normal default manager only.
        self._dsh = dsh_adapter if dsh_adapter is not None else (None if status_path is not None else DshApprovalAdapter())

    @staticmethod
    def _process_running(tool):
        needles = {"dsh": ("deepseek-harness", "dsh"), "codex": ("codex",)}[tool]
        try:
            for process in psutil.process_iter(("name", "cmdline")):
                name = (process.info.get("name") or "").lower()
                command = " ".join(process.info.get("cmdline") or ()).lower()
                if any(needle in name or needle in command for needle in needles):
                    return True
        except (OSError, psutil.Error):
            pass
        return False

    @classmethod
    def _entry(cls, tool, raw):
        if not isinstance(raw, dict):
            return None
        state = raw.get("state", "idle")
        request_id = raw.get("id", "")
        text = raw.get("text", "")
        if tool not in cls.TOOLS or state not in cls.STATES or not isinstance(request_id, str) or \
                not isinstance(text, str) or len(request_id) > 36 or len(text) > 48:
            return None
        if state == "ask" and not request_id:
            return None
        return {"tool": tool, "state": state, "id": request_id, "text": text}

    def collect(self):
        entries = {}
        live_dsh = self._dsh.snapshot() if self._dsh is not None else None
        if live_dsh:
            entries["dsh"] = live_dsh
        if self.status_path.is_file():
            try:
                raw = json.loads(self.status_path.read_text(encoding="utf-8"))
                raw = raw.get("agents", raw) if isinstance(raw, dict) else raw
                if isinstance(raw, dict):
                    for tool in self.TOOLS:
                        item = self._entry(tool, raw.get(tool))
                        if item:
                            entries[tool] = item
                elif isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict):
                            parsed = self._entry(item.get("tool"), item)
                            if parsed:
                                entries[parsed["tool"]] = parsed
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        for tool in self.TOOLS:
            if tool not in entries:
                entries[tool] = {"tool": tool, "state": "running" if self._process_running(tool) else "offline",
                                 "id": "", "text": ""}
            # A live DSH event stream is authoritative.  In particular, do
            # not let the immediate post-action `done` marker hide a later
            # approval/request delivered by the same stream.
            if tool in self._overrides and not (tool == "dsh" and self._dsh is not None):
                entries[tool] = dict(self._overrides[tool])
        return [entries[tool] for tool in self.TOOLS]

    def close(self):
        if self._dsh is not None:
            self._dsh.close()

    @staticmethod
    def _desktop_key(tool, action):
        if os.name != "nt":
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, len(title))
            expected = "deepseek" if tool == "dsh" else "codex"
            if expected not in title.value.lower():
                return False
            key = 0x0D if action == "approve" else 0x1B
            user32.keybd_event(key, 0, 0, 0)
            user32.keybd_event(key, 0, 2, 0)
            return True
        except (AttributeError, OSError):
            return False

    def action(self, tool, request_id, action):
        current = {item["tool"]: item for item in self.collect()}.get(tool)
        if not current or current["state"] != "ask" or current["id"] != request_id:
            return False
        if tool == "dsh" and self._dsh is not None and self._dsh.decide(request_id, action):
            accepted = True
        else:
            accepted = self._desktop_key(tool, action)
        if not accepted:
            return False
        try:
            self.action_path.parent.mkdir(parents=True, exist_ok=True)
            with self.action_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"tool": tool, "id": request_id, "action": action,
                                         "time": datetime.now().isoformat(timespec="seconds")},
                                        separators=(",", ":")) + "\n")
        except OSError:
            return False
        if self._dsh is None or tool != "dsh":
            self._overrides[tool] = {"tool": tool, "state": "done", "id": "", "text": action.upper()}
        return True


class _WebSocket:
    """Small text-frame WebSocket client for DSH's local Remote mux."""

    def __init__(self, host, port, cookie, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET /api/remote.mux HTTP/1.1\r\nHost: {host}:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Cookie: {cookie}\r\n\r\n").encode("ascii")
        self.sock.sendall(request)
        response = self._read_http_headers()
        if not response.startswith(b"HTTP/1.1 101"):
            self.close()
            raise OSError("DSH Remote mux rejected WebSocket upgrade")

    def _read_http_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("DSH Remote mux closed during WebSocket upgrade")
            data.extend(chunk)
            if len(data) > 16384:
                raise OSError("DSH Remote mux response headers are too large")
        return bytes(data)

    def send(self, value):
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes((0x81, 0x80 | size))
        elif size <= 0xffff:
            header = bytes((0x81, 0xfe)) + struct.pack("!H", size)
        else:
            header = bytes((0x81, 0xff)) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self):
        first, second = self._read_exact(2)
        opcode = first & 0x0f
        length = second & 0x7f
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        masked = second & 0x80
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x9:
            self._send_control(0xA, payload)
            return self.recv()
        if opcode in (0x8, 0xA):
            return None
        if opcode != 0x1:
            return self.recv()
        return json.loads(payload.decode("utf-8"))

    def _send_control(self, opcode, payload=b""):
        size = len(payload)
        if size >= 126:
            return
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | size)) + mask + masked)

    def _read_exact(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise OSError("DSH Remote mux disconnected")
            data.extend(chunk)
        return bytes(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class DshApprovalAdapter:
    """Read and answer live DSH approval/request Remote Events."""

    def __init__(self, host=None, port=None, credentials_path=None):
        self.host = host or os.environ.get("XTE_DSH_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("XTE_DSH_PORT", "3080"))
        self.credentials_path = Path(credentials_path or
                                     (Path(os.environ.get("USERPROFILE", Path.home())) / ".dsh" / ".credentials.yaml"))
        self._lock = threading.Lock()
        self._pending = None
        self._stop = threading.Event()
        self._client_id = ""
        self._thread = threading.Thread(target=self._run, name="xte-dsh-approval", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def snapshot(self):
        with self._lock:
            pending = dict(self._pending) if self._pending else None
        if not pending:
            return None
        return {"tool": "dsh", "state": "ask", "id": pending["event_id"],
                "text": pending["text"]}

    def decide(self, request_id, action):
        if action not in ("approve", "reject"):
            return False
        with self._lock:
            pending = dict(self._pending) if self._pending else None
        if not pending or pending["event_id"] != request_id:
            return False
        outcome = "allowed-once" if action == "approve" else "rejected"
        try:
            self._post_result(pending["client_id"], request_id, outcome)
        except (OSError, ValueError, urllib.error.URLError):
            return False
        with self._lock:
            if self._pending and self._pending["event_id"] == request_id:
                self._pending = None
        return True

    def _run(self):
        while not self._stop.is_set():
            ws = None
            try:
                cookie = self._cookie()
                if not cookie:
                    self._stop.wait(2)
                    continue
                ws = _WebSocket(self.host, self.port, cookie, timeout=2.0)
                stream_id = str(uuid.uuid4())
                ws.send({"type": "open", "streamId": stream_id, "endpoint": "$events",
                         "payload": {"args": {}}})
                while not self._stop.is_set():
                    try:
                        value = ws.recv()
                    except socket.timeout:
                        continue
                    if value is None:
                        break
                    self._consume(value)
            except (OSError, ValueError, json.JSONDecodeError):
                self._stop.wait(2)
            finally:
                if ws:
                    ws.close()

    def _consume(self, value):
        if value.get("type") != "item":
            return
        frame = value.get("value")
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "item":
            frame = frame.get("value")
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type == "ready":
            with self._lock:
                self._client_id = frame.get("clientId", "")
            return
        if frame_type == "cancel":
            with self._lock:
                if self._pending and self._pending["event_id"] == frame.get("eventId"):
                    self._pending = None
            return
        if frame_type != "waterfall" or frame.get("event") != "approval/request":
            return
        request = frame.get("request") if isinstance(frame.get("request"), dict) else {}
        reason = request.get("displayReason") or request.get("reason") or request.get("toolName") or "PENDING APPROVAL"
        if isinstance(reason, dict):
            reason = reason.get("en") or next(iter(reason.values()), "PENDING APPROVAL")
        text = str(reason).split(":", 1)[0]
        text = text.encode("ascii", "replace").decode("ascii")
        if not text.strip(" ?"):
            text = str(request.get("toolName") or "PENDING APPROVAL")
        text = text.replace("\r", " ").replace("\n", " ")[:48]
        with self._lock:
            self._pending = {"event_id": str(frame.get("eventId", "")),
                             "client_id": str(self._client_id), "text": text}

    def _post_result(self, client_id, event_id, outcome):
        cookie = self._cookie()
        if not cookie:
            raise OSError("DSH credentials unavailable")
        message = {"type": "client-request", "rpcId": str(uuid.uuid4()),
                   "method": "$events/result",
                   "payload": {"args": {"clientId": client_id, "eventId": event_id,
                                          "outcome": {"kind": "result", "value": outcome}}}}
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/api/$events/result",
            data=json.dumps(message, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie}, method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                raise OSError(f"DSH approval result HTTP {response.status}")

    def _cookie(self):
        try:
            content = self.credentials_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        match = re.search(r"(?ms)^\s*secret:\s*([A-Za-z0-9_-]{43})\s*$", content)
        if not match:
            return None
        secret = base64.urlsafe_b64decode(match.group(1) + "=")
        authority = f"{self.host}:{self.port}"
        issued = int(time.time() * 1000)
        expires = issued + 30 * 24 * 60 * 60 * 1000
        body = base64.urlsafe_b64encode(json.dumps(
            {"version": 1, "authority": authority, "issuedAt": issued, "expiresAt": expires},
            separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()).decode("ascii").rstrip("=")
        value = f"v1.{body}.{signature}"
        name = "dsh-auth-" + base64.urlsafe_b64encode(hashlib.sha256(authority.encode("ascii")).digest()).decode("ascii").rstrip("=")
        return f"{name}={value}"


class WindowsVolume:
    """Read and adjust the default Windows endpoint master volume."""

    def __init__(self):
        self._endpoint = None
        self._enumerator = None
        self._ok = False
        if sys.platform != "win32":
            return
        try:
            import comtypes
            from comtypes import CLSCTX_ALL, CoCreateInstance
            from comtypes.client import CreateObject
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._endpoint = ctypes.cast(interface, ctypes.POINTER(IAudioEndpointVolume))
            self._ok = True
        except (ImportError, OSError, RuntimeError, TypeError):
            self._ok = False

    def get_percent(self):
        if not self._ok:
            return None
        try:
            return round(max(0.0, min(1.0, self._endpoint.GetMasterVolumeLevelScalar())) * 100, 1)
        except (OSError, ValueError):
            return None

    def adjust(self, delta):
        if not self._ok:
            return None
        try:
            current = self._endpoint.GetMasterVolumeLevelScalar()
            target = max(0.0, min(1.0, current + delta / 100.0))
            self._endpoint.SetMasterVolumeLevelScalar(target, None)
            return self.get_percent()
        except (OSError, ValueError):
            return None


def collect_status(volume=None, agents=None):
    host = socket.gethostname().encode("ascii", "replace").decode("ascii")[:32] or "PC"
    status = {"v": 2, "host": host, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "cpu": None, "gpu": None, "gpu_temp_c": None,
              "mem_used_mib": None, "mem_total_mib": None,
              "volume": volume.get_percent() if volume else None}
    if agents is not None:
        status["agents"] = agents
    try:
        status["gpu"], status["gpu_temp_c"] = collect_amd_gpu()
    except (OSError, AttributeError) as exc:
        print(f"AMD GPU unavailable: {exc}", file=sys.stderr)
    if status["gpu"] is None and status["gpu_temp_c"] is None:
        try:
            nvidia = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            rows = [line.split(",") for line in nvidia.stdout.splitlines() if line.strip()]
            if nvidia.returncode == 0 and len(rows) == 1:
                load, temperature = (float(value.strip()) for value in rows[0])
                if 0 <= load <= 100:
                    status["gpu"] = load
                if -50 <= temperature <= 150:
                    status["gpu_temp_c"] = temperature
        except FileNotFoundError:
            pass
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"GPU unavailable: {exc}", file=sys.stderr)
    try:
        status["cpu"] = round(psutil.cpu_percent(interval=None), 1)
    except (OSError, psutil.Error) as exc:
        print(f"CPU unavailable: {exc}", file=sys.stderr)
    try:
        memory = psutil.virtual_memory()
        status["mem_total_mib"] = round(memory.total / (1024 ** 2), 1)
        status["mem_used_mib"] = round((memory.total - memory.available) / (1024 ** 2), 1)
    except (OSError, psutil.Error) as exc:
        print(f"Memory unavailable: {exc}", file=sys.stderr)
    return status


def demo_status():
    return {"v": 2, "host": "DEMO-PC", "time": "2026-10-10 12:34:56", "cpu": 12.5,
            "gpu": 38.0, "gpu_temp_c": 51.0, "mem_used_mib": 7168, "mem_total_mib": 16384,
            "volume": 55.0, "agents": [
                {"tool": "dsh", "state": "idle", "id": "", "text": ""},
                {"tool": "codex", "state": "idle", "id": "", "text": ""},
            ]}


def open_serial(port):
    # Set control lines before opening to avoid an intentional device reset.
    connection = serial.serial_for_url(port, baudrate=115200, timeout=0, write_timeout=2, do_not_open=True)
    connection.dtr = False
    connection.rts = False
    connection.open()
    return connection


def volume_confirmation(line, volume, snapshot):
    parts = line.strip().split()
    if len(parts) not in (3, 4) or parts[:2] != ["XTE:", "volume_delta"]:
        return None
    try:
        delta = int(parts[2])
        sequence = int(parts[3]) if len(parts) == 4 else 0
    except ValueError:
        return None
    if delta not in (-10, -5, -1, 1, 5, 10) or not 0 <= sequence <= 0xffffffff:
        return None
    confirmed = dict(snapshot)
    confirmed["volume"] = volume.adjust(delta)
    confirmed["volume_ack"] = sequence
    return confirmed


def agent_confirmation(line, manager, snapshot):
    parts = line.strip().split()
    if len(parts) != 5 or parts[:2] != ["XTE:", "agent_action"]:
        return None
    action, tool, request_id = parts[2:]
    if action not in ("approve", "reject") or tool not in manager.TOOLS or not request_id or len(request_id) > 36:
        return None
    if not manager.action(tool, request_id, action):
        return None
    confirmed = dict(snapshot)
    confirmed["agents"] = manager.collect()
    return confirmed


def main(argv=None, stop_event=None, on_connection=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="COM port (e.g. COM3); omit to print JSON")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--demo", action="store_true", help="use fixed sample data")
    parser.add_argument("--once", action="store_true", help="send/print one frame and exit")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    def wait(seconds):
        if stop_event is None:
            time.sleep(seconds)
        else:
            stop_event.wait(seconds)

    def report(connected):
        if on_connection is not None:
            on_connection(connected, args.port)
    if args.list_ports:
        for port in list_ports.comports():
            print(f"{port.device}: {port.description}")
        return 0
    if not 5 <= args.interval <= 10:
        parser.error("--interval must be between 5 and 10 seconds (device timeout: 15 seconds)")
    if not args.demo:
        try:
            psutil.cpu_percent(interval=None)
        except (OSError, psutil.Error):
            pass
        wait(1)  # Discard psutil's first nonblocking CPU sample.
    connection = None
    volume = WindowsVolume()
    agents = AgentManager()
    next_sample = 0.0
    pending_once = False
    exit_at = 0.0
    device_output = ""
    snapshot = None
    try:
        while stop_event is None or not stop_event.is_set():
            now = time.monotonic()
            if args.port and connection is None:
                try:
                    connection = open_serial(args.port)
                    next_sample = 0.0
                    device_output = ""
                    print(f"Connected: {args.port}", file=sys.stderr)
                    report(True)
                except (OSError, serial.SerialException) as exc:
                    print(f"Serial unavailable: {exc}", file=sys.stderr)
                    report(False)
                    if args.once:
                        return 1
                    wait(1)
                    continue
            try:
                # Drain SDK logs so USB transmit buffers cannot block refreshes.
                if connection and connection.in_waiting:
                    data = connection.read(min(connection.in_waiting, 4096))
                    text = data.decode("utf-8", "replace")
                    sys.stderr.write(text)
                    device_output += text
                    lines = device_output.split("\n")
                    device_output = lines.pop()
                    if len(device_output) > 4096:
                        device_output = ""
                    for line in lines:
                        if not args.demo and snapshot is not None:
                            confirmed = volume_confirmation(line, volume, snapshot)
                            if confirmed is not None:
                                frame = encode_status(confirmed)
                                if connection.write(frame) != len(frame):
                                    raise serial.SerialException("incomplete serial write")
                                snapshot = confirmed
                                print(f"Volume confirmed: {confirmed['volume']}%", file=sys.stderr)
                            confirmed = agent_confirmation(line, agents, snapshot)
                            if confirmed is not None:
                                frame = encode_status(confirmed)
                                if connection.write(frame) != len(frame):
                                    raise serial.SerialException("incomplete serial write")
                                snapshot = confirmed
                                print("Agent action accepted", file=sys.stderr)
                if not pending_once and now >= next_sample:
                    snapshot = demo_status() if args.demo else collect_status(volume, agents.collect())
                    frame = encode_status(snapshot)
                    if connection:
                        if connection.write(frame) != len(frame):
                            raise serial.SerialException("incomplete serial write")
                    else:
                        print(frame.decode("ascii").rstrip(), flush=True)
                    next_sample = now + args.interval
                    if args.once:
                        if connection is None:
                            return 0
                        pending_once, exit_at = True, now + 5
                if pending_once and now >= exit_at:
                    return 0
            except (OSError, serial.SerialException) as exc:
                print(f"Serial disconnected: {exc}", file=sys.stderr)
                if connection:
                    connection.close()
                connection = None
                report(False)
                if args.once:
                    return 1
            wait(0.05)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if connection:
            connection.close()
        agents.close()


if __name__ == "__main__":
    raise SystemExit(main())
