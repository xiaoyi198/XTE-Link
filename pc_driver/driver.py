"""Collect PC metrics and send bounded JSON lines to an XTE-Link device."""

import argparse
from datetime import datetime
import json
import queue
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
import ipaddress
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
    from .protocol import encode_status, encode_agent
    from .amd_gpu import collect_amd_gpu
    from .codex_ipc import CodexApprovalAdapter
    from .clash_panel import Worker as ClashWorker
else:
    from protocol import encode_status, encode_agent
    from amd_gpu import collect_amd_gpu
    from codex_ipc import CodexApprovalAdapter
    from pc_driver.clash_panel import Worker as ClashWorker


class AgentManager:
    """Bridge desktop approval state and guarded keyboard actions."""

    TOOLS = ("dsh", "codex")
    STATES = ("offline", "idle", "running", "ask", "done", "error")

    def __init__(self, status_path=None, action_path=None, dsh_adapter=None, codex_adapter=None):
        default_root = Path(os.environ.get("USERPROFILE", Path.home())) / ".xte-link"
        self.status_path = Path(status_path or os.environ.get("XTE_AGENT_STATUS", default_root / "agents.json"))
        self.action_path = Path(action_path or os.environ.get("XTE_AGENT_ACTIONS", default_root / "actions.jsonl"))
        self._overrides = {}
        self._override_requests = {}
        self._handled = set()
        self._process_cache = {}
        self._process_at = 0
        self.changed = threading.Event()
        self._lock = threading.RLock()
        # Explicit test/bridge paths stay deterministic; the live adapter is
        # enabled for the normal default manager only.
        self._dsh = dsh_adapter if dsh_adapter is not None else (None if status_path is not None else DshApprovalAdapter(changed=self.changed))
        self._codex = codex_adapter if codex_adapter is not None else (None if status_path is not None else CodexApprovalAdapter(changed=self.changed))

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
        if any(not 32 <= ord(ch) <= 126 for ch in request_id + text):
            return None
        if state == "ask" and not request_id:
            return None
        return {"tool": tool, "state": state, "id": request_id, "text": text}

    def collect(self):
        with self._lock:
            return self._collect()

    def _collect(self):
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
        # Process existence is only a fallback, and expensive to query on every
        # file/event check. Live requests and the bridge take precedence.
        now = time.monotonic()
        if not self._process_cache or now >= self._process_at:
            self._process_cache = {tool: self._process_running(tool) for tool in self.TOOLS}
            self._process_at = now + 5
        if live_dsh:
            entries["dsh"] = live_dsh
        if self._codex is not None:
            entries["codex"] = self._codex.snapshot()
        for tool in self.TOOLS:
            if tool not in entries:
                entries[tool] = {"tool": tool, "state": "running" if self._process_cache[tool] else "offline",
                                 "id": "", "text": ""}
            # A live DSH event stream is authoritative.  In particular, do
            # not let the immediate post-action `done` marker hide a later
            # approval/request delivered by the same stream.
            if tool in self._overrides and not ((tool == "dsh" and self._dsh is not None) or
                                               (tool == "codex" and self._codex is not None)):
                if entries[tool]["state"] == "ask" and entries[tool]["id"] != self._override_requests.get(tool, entries[tool]["id"]):
                    del self._overrides[tool]
                else:
                    entries[tool] = dict(self._overrides[tool])
        return [entries[tool] for tool in self.TOOLS]

    def close(self):
        if self._dsh is not None:
            self._dsh.close()
        if self._codex is not None:
            self._codex.close()

    @staticmethod
    def _desktop_key(tool, action):
        if os.name != "nt" or tool != "dsh":
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, len(title))
            expected = "deepseek"
            if expected not in title.value.lower():
                return False
            key = 0x0D if action == "approve" else 0x1B
            user32.keybd_event(key, 0, 0, 0)
            user32.keybd_event(key, 0, 2, 0)
            return True
        except (AttributeError, OSError):
            return False

    def action(self, tool, request_id, action):
        with self._lock:
            return self._action(tool, request_id, action)

    def _action(self, tool, request_id, action):
        if action not in ("approve", "reject"):
            return False
        if (tool, request_id) in self._handled:
            return False
        current = {item["tool"]: item for item in self.collect()}.get(tool)
        if not current or current["state"] != "ask" or current["id"] != request_id:
            return False
        if tool == "codex":
            accepted = self._codex is not None and self._codex.decide(request_id, action)
        elif tool == "dsh" and self._dsh is not None:
            # A live adapter owns the request. A rejected/failed submission
            # must never turn into an unrelated foreground-window keypress.
            try:
                accepted = self._dsh.decide(request_id, action)
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"DSH approval {action} {request_id!r} failed: {type(exc).__name__}", file=sys.stderr)
                return False
        else:
            accepted = self._desktop_key(tool, action)
        if not accepted:
            if tool == "dsh" and self._dsh is not None:
                print(f"DSH approval {action} {request_id!r} failed: adapter did not accept request", file=sys.stderr)
            return False
        self._handled.add((tool, request_id))
        if len(self._handled) > 128:
            self._handled = {(tool, request_id)}
        try:
            self.action_path.parent.mkdir(parents=True, exist_ok=True)
            with self.action_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"tool": tool, "id": request_id, "action": action,
                                         "time": datetime.now().isoformat(timespec="seconds")},
                                        separators=(",", ":")) + "\n")
        except OSError:
            print("Agent action accepted; action log unavailable", file=sys.stderr)
        if tool == "dsh" and self._dsh is None:
            with self._lock:
                self._override_requests[tool] = request_id
                self._overrides[tool] = {"tool": tool, "state": "done", "id": "", "text": action.upper()}
        self.changed.set()
        return True


def _dsh_loopback_host(host):
    if isinstance(host, str) and host.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("DSH host must be a loopback IP address or localhost") from None
    if not address.is_loopback:
        raise ValueError("DSH host must be a loopback IP address or localhost")
    return str(address)


def _dsh_authority(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _DshNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Cookie-bearing requests stay on the validated endpoint, including
        # redirects to another port or another local service.
        raise urllib.error.HTTPError(request.full_url, code, "DSH redirects disabled", headers, response)


class _WebSocket:
    """Small text-frame WebSocket client for DSH's local Remote mux."""

    def __init__(self, host, port, cookie, timeout=2.0):
        host = _dsh_loopback_host(host)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET /api/remote.mux HTTP/1.1\r\nHost: {_dsh_authority(host, port)}\r\n"
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

    def __init__(self, host=None, port=None, credentials_path=None, changed=None):
        self.host = _dsh_loopback_host(host or os.environ.get("XTE_DSH_HOST", "127.0.0.1"))
        self.port = int(port or os.environ.get("XTE_DSH_PORT", "3080"))
        self.credentials_path = Path(credentials_path or
                                     (Path(os.environ.get("USERPROFILE", Path.home())) / ".dsh" / ".credentials.yaml"))
        self._lock = threading.Lock()
        self._pending = None
        self.changed = changed or threading.Event()
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
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # Exception messages/URLs can include credentials. Keep only the
            # diagnostic type, HTTP status or OS error code in the log.
            cause = exc.reason if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception) else exc
            reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(cause).__name__
            if isinstance(cause, OSError) and cause.errno is not None:
                reason += f" (errno {cause.errno})"
            print(f"DSH approval {action} {request_id!r} failed: {reason}", file=sys.stderr)
            return False
        with self._lock:
            if not self._pending or self._pending["event_id"] != request_id or \
                    self._pending["client_id"] != pending["client_id"]:
                print(f"DSH approval {action} {request_id!r} unconfirmed: request ended or changed", file=sys.stderr)
                return False
            self._pending = None
        self.changed.set()
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
                # Requests belong to this stream/client. Never expose one
                # from a disconnected stream as an actionable old approval.
                with self._lock:
                    had_pending = self._pending is not None
                    self._pending = None
                    self._client_id = ""
                if had_pending:
                    self.changed.set()

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
            self.changed.set()
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
        self.changed.set()

    def _post_result(self, client_id, event_id, outcome):
        host = _dsh_loopback_host(self.host)
        cookie = self._cookie()
        if not cookie:
            raise OSError("DSH credentials unavailable")
        message = {"type": "client-request", "rpcId": str(uuid.uuid4()),
                   "method": "$events/result",
                   "payload": {"args": {"clientId": client_id, "eventId": event_id,
                                          "outcome": {"kind": "result", "value": outcome}}}}
        request = urllib.request.Request(
            f"http://{_dsh_authority(host, self.port)}/api/$events/result",
            data=json.dumps(message, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _DshNoRedirect())
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                raise OSError(f"DSH approval result HTTP {response.status}")

    def _cookie(self):
        host = _dsh_loopback_host(self.host)
        try:
            content = self.credentials_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        match = re.search(r"(?ms)^\s*secret:\s*([A-Za-z0-9_-]{43})\s*$", content)
        if not match:
            return None
        secret = base64.urlsafe_b64decode(match.group(1) + "=")
        authority = _dsh_authority(host, self.port)
        issued = int(time.time() * 1000)
        expires = issued + 30 * 24 * 60 * 60 * 1000
        body = base64.urlsafe_b64encode(json.dumps(
            {"version": 1, "authority": authority, "issuedAt": issued, "expiresAt": expires},
            separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        signature = base64.urlsafe_b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()).decode("ascii").rstrip("=")
        value = f"v1.{body}.{signature}"
        name = "dsh-auth-" + base64.urlsafe_b64encode(hashlib.sha256(authority.encode("ascii")).digest()).decode("ascii").rstrip("=")
        return f"{name}={value}"


try:
    from _ctypes import COMError
except ImportError:  # Not Windows: COM is never used.
    class COMError(Exception):
        pass


class WindowsVolume:
    """Read and adjust the default Windows endpoint master volume."""

    def __init__(self):
        self._endpoint = None
        self._ok = False
        if sys.platform == "win32":
            self._connect()

    def _connect(self):
        # Audio device changes invalidate the endpoint; callers re-acquire it once.
        self._ok = False
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._endpoint = ctypes.cast(interface, ctypes.POINTER(IAudioEndpointVolume))
            self._ok = True
        except (ImportError, OSError, RuntimeError, TypeError, COMError):
            self._ok = False

    def _call(self, action):
        for attempt in range(2):
            if not self._ok:
                return None
            try:
                return action(self._endpoint)
            except (OSError, ValueError, COMError):
                if attempt:
                    return None
                self._connect()
        return None

    def get_percent(self):
        return self._call(lambda endpoint: round(max(0.0, min(1.0, endpoint.GetMasterVolumeLevelScalar())) * 100, 1))

    def adjust(self, delta):
        def change(endpoint):
            target = max(0.0, min(1.0, endpoint.GetMasterVolumeLevelScalar() + delta / 100.0))
            endpoint.SetMasterVolumeLevelScalar(target, None)
            return round(max(0.0, min(1.0, endpoint.GetMasterVolumeLevelScalar())) * 100, 1)
        return self._call(change)


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


def volume_confirmation(line, volume, snapshot, seen=None):
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
    if sequence and seen is not None and sequence in seen:
        previous_delta, confirmed["volume"] = seen[sequence]
        if previous_delta != delta:
            return None
    else:
        confirmed["volume"] = volume.adjust(delta)
        if sequence and seen is not None:
            seen[sequence] = (delta, confirmed["volume"])
            if len(seen) > 128:
                del seen[next(iter(seen))]
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


class MetricSampler:
    """One bounded worker; slow GPU/ADL sampling never owns the transport."""
    def __init__(self, wake):
        self.wake = wake
        self.requested = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.result = None
        self.thread = threading.Thread(target=self._run, name="xte-sensors", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            psutil.cpu_percent(interval=None)
            self.stopped.wait(1)
            while not self.stopped.is_set():
                if not self.requested.wait(0.1):
                    continue
                self.requested.clear()
                try:
                    result = collect_status()
                except Exception as exc:
                    print(f"Sensor collection failed: {type(exc).__name__}", file=sys.stderr)
                    continue
                with self.lock:
                    self.result = result
                self.wake.set()
        finally:
            self.wake.set()

    def request(self):
        self.requested.set()

    def take(self):
        with self.lock:
            result, self.result = self.result, None
            return result

    def close(self):
        self.stopped.set(); self.requested.set()
        self.thread.join(timeout=3)


class AgentFeed:
    """Event wakeups for DSH; 250ms checks for the existing JSON bridge."""
    def __init__(self, manager, wake):
        self.manager, self.wake = manager, wake
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.entries = []
        self.thread = threading.Thread(target=self._run, name="xte-agent-feed", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            self.manager.changed.clear()
            try:
                entries = self.manager.collect()
                with self.lock:
                    changed = entries != self.entries
                    self.entries = entries
                if changed:
                    self.wake.set()
            except (OSError, ValueError) as exc:
                print(f"Agent collection failed: {type(exc).__name__}", file=sys.stderr)
            self.manager.changed.wait(0.25)

    def get(self):
        with self.lock:
            return [dict(item) for item in self.entries]

    def close(self):
        self.stopped.set(); self.manager.changed.set()
        self.thread.join(timeout=3)


def advance_deadline(deadline, now, interval):
    """Skip missed ticks without drift, bursts, or moving ticks on AI events."""
    if deadline <= now:
        deadline += (int((now - deadline) // interval) + 1) * interval
    return deadline


def status_frame(snapshot, events):
    value = dict(snapshot)
    if events:
        value.pop("agents", None)
    try:
        return encode_status(value)
    except ValueError:
        # Older firmware cannot receive split agent events. Retain telemetry
        # and report the compatibility limit instead of killing the worker.
        if events or not value.get("agents"):
            raise
        value.pop("agents")
        print("Legacy firmware agent frame too large; update firmware for split events", file=sys.stderr)
        return encode_status(value)


def main(argv=None, stop_event=None, on_connection=None, on_device=None, commands=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="COM port (e.g. COM3); omit to print JSON")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--demo", action="store_true", help="use fixed sample data")
    parser.add_argument("--once", action="store_true", help="send/print one frame and exit")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--mode", choices=("auto", "usb", "lan"), help="follow firmware, USB only, or LAN only")
    parser.add_argument("--host", help="manual LAN address; still requires a USB-paired identity")
    parser.add_argument("--config", help="driver configuration path")
    args = parser.parse_args(argv)
    def wait(seconds):
        if stop_event is None:
            time.sleep(seconds)
        else:
            wake.wait(seconds)
            wake.clear()
    if args.list_ports:
        for port in list_ports.comports():
            print(f"{port.device}: {port.description}")
        return 0
    if not 5 <= args.interval <= 3600:
        parser.error("--interval must be between 5 and 3600 seconds")
    wake = threading.Event()
    if not args.demo and args.once:
        try:
            psutil.cpu_percent(interval=None)
        except (OSError, psutil.Error):
            pass
        wait(1)  # Discard psutil's first nonblocking CPU sample.
    from pc_driver.network import Session, config_path
    session = Session(open_serial, args.config or config_path(), args.mode or "usb", args.port,
                      args.host, on_device) if args.port or args.mode or args.host else None
    volume = WindowsVolume()
    agents = AgentManager()
    sampler = MetricSampler(wake) if not args.demo and not args.once else None
    feed = AgentFeed(agents, wake) if not args.demo and not args.once else None
    next_sample = time.monotonic()
    next_heartbeat = 0.0
    epoch = -1
    source = None
    reported = None
    sent_agents = {}
    seen_volume = {}
    pending_once = False
    exit_at = 0.0
    device_output = ""
    snapshot = None
    clash_worker = ClashWorker() if session and not args.demo else None
    try:
        while stop_event is None or not stop_event.is_set():
            now = time.monotonic()
            try:
                if session:
                    session.tick()
                    state = (bool(session.active), session.label)
                    if state != reported:
                        reported = state
                        if on_connection:
                            on_connection(*state)
                    if commands:
                        try:
                            job, callback = commands.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            try:
                                job(session)
                                callback(None)
                            except (OSError, ValueError) as exc:
                                callback(str(exc))
                    if session.epoch != epoch:
                        epoch = session.epoch
                        sent_agents.clear()
                        if source is not session.active:
                            device_output = ""
                            seen_volume.clear()
                            next_sample = now
                        source = session.active
                        next_heartbeat = 0
                        if snapshot and session.active:
                            session.write(status_frame(snapshot, session.supports_events))
                    if not session.active:
                        if args.once:
                            return 1
                        wait(0.05)
                        continue
                # Drain SDK logs so USB transmit buffers cannot block refreshes.
                if session and session.in_waiting:
                    data = session.read(min(session.in_waiting, 4096))
                    text = data.decode("utf-8", "replace")
                    sys.stderr.write(text)
                    device_output += text
                    lines = device_output.split("\n")
                    device_output = lines.pop()
                    if len(device_output) > 4096:
                        device_output = ""
                    for line in lines:
                        if clash_worker and clash_worker.handle(line, session.epoch):
                            continue
                        if not args.demo and snapshot is not None:
                            confirmed = volume_confirmation(line, volume, snapshot, seen_volume)
                            if confirmed is not None:
                                frame = status_frame(confirmed, session.supports_events)
                                if session.write(frame) != len(frame):
                                    raise serial.SerialException("incomplete serial write")
                                snapshot = confirmed
                                print(f"Volume confirmed: {confirmed['volume']}%", file=sys.stderr)
                            confirmed = agent_confirmation(line, agents, snapshot)
                            if confirmed is not None:
                                frame = status_frame(confirmed, session.supports_events)
                                if session.write(frame) != len(frame):
                                    raise serial.SerialException("incomplete serial write")
                                snapshot = confirmed
                                if session.supports_events:
                                    for entry in confirmed["agents"]:
                                        session.write(encode_agent(entry))
                                    sent_agents = {entry["tool"]: entry for entry in confirmed["agents"]}
                                print("Agent action accepted", file=sys.stderr)
                if clash_worker and session and session.active:
                    for frame in clash_worker.take(session.epoch):
                        session.write(frame)
                if not pending_once and now >= next_sample:
                    next_sample = advance_deadline(next_sample, now, args.interval)
                    if sampler:
                        sampler.request()
                    else:
                        snapshot = demo_status() if args.demo else collect_status(volume, agents.collect())
                        if session:
                            session.write(status_frame(snapshot, session.supports_events))
                        else:
                            print(encode_status(snapshot).decode("ascii").rstrip(), flush=True)
                        if args.once:
                            if session is None:
                                return 0
                            pending_once, exit_at = True, now + 5
                if sampler:
                    sample = sampler.take()
                    if sample:
                        sample["volume"] = volume.get_percent()
                        if snapshot and "volume_ack" in snapshot:
                            sample["volume_ack"] = snapshot["volume_ack"]
                        sample["agents"] = feed.get()
                        snapshot = sample
                        if session:
                            session.write(status_frame(snapshot, session.supports_events))
                        else:
                            print(encode_status(snapshot).decode("ascii").rstrip(), flush=True)
                entries = demo_status()["agents"] if args.demo else feed.get() if feed else agents.collect()
                # A watcher snapshot collected before a successful action must
                # not resurrect that request while its next check is pending.
                entries = [sent_agents.get(entry["tool"], entry)
                           if entry["state"] == "ask" and (entry["tool"], entry["id"]) in agents._handled
                           else entry for entry in entries]
                if session:
                    if session.supports_events:
                        for entry in entries:
                            if sent_agents.get(entry["tool"]) != entry:
                                session.write(encode_agent(entry))
                                sent_agents[entry["tool"]] = dict(entry)
                    elif snapshot and entries != list(sent_agents.values()):
                        snapshot["agents"] = entries
                        if sent_agents:
                            session.write(status_frame(snapshot, False))
                        sent_agents = {entry["tool"]: dict(entry) for entry in entries}
                    if session.supports_events and now >= next_heartbeat:
                        session.write(b'{"v":2,"cmd":"heartbeat"}\n')
                        next_heartbeat = now + 5
                if pending_once and now >= exit_at:
                    return 0
            except (OSError, serial.SerialException) as exc:
                print(f"Connection unavailable: {exc}", file=sys.stderr)
                if args.once:
                    return 1
            wait(0.05)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if clash_worker:
            clash_worker.close()
        if session:
            session.close()
        if sampler:
            sampler.close()
        if feed:
            feed.close()
        agents.close()


if __name__ == "__main__":
    raise SystemExit(main())
