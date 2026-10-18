"""USB enrollment, mDNS discovery and authenticated, bounded LAN transport."""

import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import struct
import threading
import time
import tempfile

from serial.tools import list_ports
from .protocol import MAX_PAYLOAD_BYTES
from .sleep_image import FOLDER as SLEEP_FOLDER
from .tf import TfClient

SERVICE = "_xte-link._tcp.local."
PORT = 8765
NEXT_CONFIG = "/xte-link/network.next.json"
CONFIG_LOCK = threading.Lock()


def config_path():
    return Path(os.environ.get("USERPROFILE", Path.home())) / ".xte-link" / "tray.json"


def load_config(path):
    result = {"port": None, "mode": "auto", "interval": 5.0, "device": "", "host": "", "devices": {}}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return result
        port = raw.get("port")
        if isinstance(port, str) and re.fullmatch(r"COM[0-9]+", port.upper()):
            result["port"] = port.upper()
        if raw.get("mode") in ("auto", "usb", "lan"):
            result["mode"] = raw["mode"]
        interval = raw.get("interval", 5)
        if type(interval) in (int, float) and 5 <= interval <= 3600:
            result["interval"] = float(interval)
        if isinstance(raw.get("host"), str) and len(raw["host"]) <= 253:
            result["host"] = raw["host"].strip()
        if isinstance(raw.get("devices"), dict):
            result["devices"] = {device: key for device, key in raw["devices"].items()
                                 if isinstance(device, str) and re.fullmatch(r"[0-9a-f]{12}", device)
                                 and isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)}
        if raw.get("device") in result["devices"]:
            result["device"] = raw["device"]
    except (OSError, ValueError, TypeError):
        pass
    return result


def save_config(path, **updates):
    with CONFIG_LOCK:
        value = load_config(path)
        value.update(updates)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return value


def wifi_config(device, key, ssid, password):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError("WiFi 名称需要 1～32 个 UTF-8 字节，不能含控制字符。")
    if not isinstance(password, str) or any(not 32 <= ord(c) <= 126 for c in password):
        raise ValueError("WiFi 密码需要可打印 ASCII 字符。")
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise ValueError("WiFi 密码需要 8～63 个字符、64 位十六进制密钥，或留空用于开放网络。")
    if not re.fullmatch(r"[0-9a-f]{12}", device) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("设备身份或配对密钥无效。")
    data = json.dumps({"v": 1, "device": device, "key": key, "ssid": ssid, "password": password},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > 512:
        raise ValueError("WiFi 配置超过设备文件大小限制。")
    return data


class PacketCodec:
    def __init__(self, key, nonce, outgoing=b"C", incoming=b"S"):
        self.key, self.nonce = key, nonce.encode("ascii")
        self.outgoing, self.incoming = outgoing, incoming
        self.tx = self.rx = 0
        self.buffer = bytearray()

    def tag(self, role, packet):
        return hmac.digest(self.key, role + b":" + self.nonce + packet, "sha256")

    def encode(self, payload):
        if not payload.endswith(b"\n") or not 1 <= len(payload) <= MAX_PAYLOAD_BYTES + 1 or self.tx == 0xffffffff:
            raise OSError("Invalid LAN payload or exhausted session")
        self.tx += 1
        packet = struct.pack("!IH", self.tx, len(payload)) + payload
        return packet + self.tag(self.outgoing, packet)

    def feed(self, data):
        self.buffer.extend(data)
        output = bytearray()
        while len(self.buffer) >= 6:
            sequence, size = struct.unpack("!IH", self.buffer[:6])
            if not 1 <= size <= MAX_PAYLOAD_BYTES + 1 or sequence != self.rx + 1:
                raise OSError("Invalid LAN sequence or size")
            count = size + 38
            if len(self.buffer) < count:
                break
            packet = bytes(self.buffer[:size + 6])
            tag = bytes(self.buffer[size + 6:count])
            if packet[-1:] != b"\n" or not hmac.compare_digest(tag, self.tag(self.incoming, packet)):
                raise OSError("LAN message authentication failed")
            self.rx = sequence
            output.extend(packet[6:])
            del self.buffer[:count]
        return bytes(output)


class TcpConnection:
    def __init__(self, host, device, key, port=PORT):
        self.sock = socket.create_connection((host, port), timeout=2)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            challenge = self._line()
            nonce = challenge.get("nonce", "")
            if challenge.get("device") != device or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
                raise OSError("LAN device identity mismatch")
            secret = bytes.fromhex(key)
            proof = hmac.digest(secret, f"client:{device}:{nonce}".encode("ascii"), "sha256").hex()
            self.sock.sendall(json.dumps({"auth": proof}, separators=(",", ":")).encode("ascii") + b"\n")
            ready = self._line()
            expected = hmac.digest(secret, f"server:{device}:{nonce}".encode("ascii"), "sha256").hex()
            if ready.get("ok") is not True or not isinstance(ready.get("proof"), str) or not hmac.compare_digest(ready["proof"], expected):
                raise OSError("LAN device authentication failed")
            self.codec = PacketCodec(secret, nonce)
            self.sock.settimeout(1)
        except Exception:
            self.sock.close()
            raise

    def _line(self):
        value = bytearray()
        while len(value) <= MAX_PAYLOAD_BYTES:
            byte = self.sock.recv(1)
            if not byte:
                raise OSError("LAN disconnected during authentication")
            if byte == b"\n":
                try:
                    result = json.loads(value)
                except (ValueError, UnicodeError) as exc:
                    raise OSError("Invalid LAN handshake") from exc
                if isinstance(result, dict):
                    return result
                break
            value.extend(byte)
        raise OSError("LAN handshake exceeds limit")

    @property
    def in_waiting(self):
        return 4096 if select.select([self.sock], [], [], 0)[0] else 0

    def read(self, size):
        data = self.sock.recv(min(size, 4096))
        if not data:
            raise OSError("LAN disconnected")
        return self.codec.feed(data)

    def write(self, frame):
        self.sock.sendall(self.codec.encode(frame))
        return len(frame)

    def close(self):
        self.sock.close()


class Discovery:
    def __init__(self):
        from zeroconf import ServiceBrowser, Zeroconf
        self.lock = threading.Lock()
        self.services = {}
        self.zc = Zeroconf()
        self.browser = ServiceBrowser(self.zc, SERVICE, handlers=[self.changed])

    def changed(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange
        value = None
        if state_change != ServiceStateChange.Removed:
            info = zeroconf.get_service_info(service_type, name, timeout=1000)
            if info:
                raw = info.properties.get(b"device", b"")
                device = raw.decode("ascii", "replace") if isinstance(raw, bytes) else ""
                addresses = [ip for ip in info.parsed_addresses() if ipaddress.ip_address(ip).version == 4]
                if re.fullmatch(r"[0-9a-f]{12}", device) and addresses and info.port == PORT:
                    value = (device, addresses[0])
        with self.lock:
            self.services.pop(name, None)
            if value:
                self.services[name] = value

    def addresses(self):
        with self.lock:
            return dict(self.services.values())

    def close(self):
        self.browser.cancel()
        self.zc.close()


class Session:
    """Keep USB for setup; only the firmware-selected transport carries data."""
    def __init__(self, open_serial, path, mode="auto", port=None, host=None, on_device=None):
        self.open_serial, self.path, self.mode = open_serial, Path(path), mode
        self.config = load_config(self.path)
        self.port, self.host = port, host or self.config["host"]
        self.usb_port = port
        self.on_device = on_device
        self.usb = self.tcp = self.discovery = None
        self.info = {}
        self.buffer = bytearray()
        self.usb_retry = self.lan_retry = self.hello_at = 0
        self.tcp_device = ""
        self.output = bytearray()
        self.last_rx = time.monotonic()
        self.epoch = 0
        self._events = False

    @property
    def active(self):
        if self.mode != "lan" and self.usb and (self.info or not self.tcp) and self.info.get("mode", "usb") == "usb":
            selected = self.config["device"]
            if not selected or not self.info or self.info.get("device") == selected:
                return self.usb
        return self.tcp

    @property
    def supports_events(self):
        return self.active is self.tcp and self.tcp is not None or self.info.get("events") == 1

    @property
    def label(self):
        if self.active is self.usb and self.usb:
            return f"USB {self.usb_port}"
        if self.tcp:
            return f"LAN {self.tcp_device}"
        if self.usb and self.info and self.mode != "auto" and self.info["mode"] != self.mode:
            return "连接方式不一致，请在设备设置中切换"
        return "等待设备或所选连接方式"

    def tick(self):
        now = time.monotonic()
        before = self.active
        if self.usb is None and now >= self.usb_retry:
            self.usb_retry = now + 1
            port = self.port
            if not port:
                candidates = [p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x303A, 0x1001)]
                port = candidates[0] if len(candidates) == 1 else None
            if port:
                try:
                    self.usb = self.open_serial(port)
                    self.usb_port = port
                    self.hello_at = 0
                except OSError:
                    pass
        if self.usb:
            try:
                if now >= self.hello_at:
                    hello = b'{"v":2,"cmd":"hello"}\n'
                    if self.usb.write(hello) != len(hello):
                        raise OSError("Incomplete USB capability query")
                    self.hello_at = now + 2
                if self.usb.in_waiting:
                    self.buffer.extend(self.usb.read(min(self.usb.in_waiting, 4096)))
                    while b"\n" in self.buffer:
                        raw, _, rest = self.buffer.partition(b"\n")
                        self.buffer = bytearray(rest)
                        if raw.startswith(b"XTE: link "):
                            self._info(raw[10:])
                        elif self.active is self.usb:
                            self.output.extend(raw + b"\n")
                            if raw.strip() in (b"XTE: pong", b"XTE: accepted"):
                                self.last_rx = now
                    if len(self.buffer) > 4096:
                        self.buffer.clear()
            except OSError:
                self.usb.close(); self.usb = None; self.info = {}; self.buffer.clear()
        selected = self.config["device"]
        selected_usb = self.usb and (self.info or not self.tcp) and (not selected or self.info.get("device") == selected or not self.info)
        want_lan = self.mode != "usb" and not (selected_usb and self.info.get("mode", "usb") == "usb")
        if self.tcp and (not want_lan or now - self.last_rx >= 15):
            self.tcp.close(); self.tcp = None
        if want_lan and self.config["devices"]:
            if self.discovery is None:
                self.discovery = Discovery()
            if not self.tcp and now >= self.lan_retry:
                self.lan_retry = now + 2
                addresses = self.discovery.addresses()
                device = self.config["device"]
                if not device and len(self.config["devices"]) == 1:
                    device = next(iter(self.config["devices"]))
                host = self.host or addresses.get(device)
                if host and device in self.config["devices"]:
                    try:
                        self.tcp = TcpConnection(host, device, self.config["devices"][device])
                        self.tcp_device = device
                        self.last_rx = now
                    except OSError:
                        pass
        if self.tcp:
            try:
                if self.tcp.in_waiting:
                    data = self.tcp.read(4096)
                    if data:
                        self.last_rx = now
                        self.output.extend(data)
            except OSError:
                self.tcp.close(); self.tcp = None
        if before is not self.active or self._events != bool(self.supports_events):
            self.epoch += 1
            if before is not self.active:
                self.output.clear()  # Old controls cannot cross into the new session.
            self.last_rx = now
        self._events = bool(self.supports_events)

    def _info(self, payload):
        try:
            info = json.loads(payload)
            if not isinstance(info, dict) or not isinstance(info.get("device"), str) or not re.fullmatch(r"[0-9a-f]{12}", info["device"]):
                return
            if info.get("mode") not in ("usb", "lan") or type(info.get("configured")) is not bool:
                return
            changed = info != self.info
            self.info = info
            if self.active is self.usb:
                self.last_rx = time.monotonic()
            if changed and self.on_device:
                self.on_device(info)
        except (ValueError, UnicodeError):
            pass

    @property
    def in_waiting(self):
        return len(self.output)

    def read(self, size):
        data = bytes(self.output[:size])
        del self.output[:size]
        return data

    def write(self, frame):
        if not self.active:
            raise OSError("No active data transport")
        try:
            size = self.active.write(frame)
            if size != len(frame):
                raise OSError("Incomplete transport write")
            return size
        except OSError:
            if self.active is self.tcp:
                self.tcp.close(); self.tcp = None
            else:
                self.usb.close(); self.usb = None; self.info = {}
            raise

    def provision(self, device, ssid, password):
        if not self.usb or not self.info.get("device"):
            raise OSError("请连接 USB，并等待设备身份确认。")
        if device != self.info["device"]:
            raise OSError("USB 设备已变化，请重新打开配置窗口。")
        key = self.config["devices"].get(device) or secrets.token_hex(32)
        data = wifi_config(device, key, ssid, password)
        # Persist the matching key before device commit, so interrupted setup
        # cannot leave a successfully configured device without its PC key.
        devices = {**load_config(self.path)["devices"], device: key}
        self.config = save_config(self.path, devices=devices, device=device)
        client = TfClient(self.usb, timeout=10)
        client.request("abort")
        client.mount()
        client.request("mkdir", path="/xte-link")
        try:
            client.request("remove", path=NEXT_CONFIG)
        except OSError as exc:
            if "not_file" not in str(exc):
                raise
        client.upload_bytes(data, NEXT_CONFIG)
        sequence = secrets.randbelow(0xffffffff) + 1
        frame = json.dumps({"v": 2, "cmd": "network_commit", "id": sequence}, separators=(",", ":")).encode("ascii") + b"\n"
        if self.usb.write(frame) != len(frame):
            raise OSError("USB 配置提交未完整发送。")
        deadline = time.monotonic() + 5
        buffer = bytearray()
        while time.monotonic() < deadline:
            data = self.usb.read(1)
            if not data:
                time.sleep(0.005)
                continue
            if data == b"\n":
                if buffer.startswith(b"XTE: link "):
                    info = json.loads(buffer[10:])
                    if info.get("id") == sequence:
                        if info.get("ok") is not True:
                            raise OSError(f"设备保存失败：{info.get('error', 'unknown')}")
                        self._info(buffer[10:])
                        self.last_rx = time.monotonic()
                        self.hello_at = 0
                        return
                buffer.clear()
            elif len(buffer) < 4096:
                buffer.extend(data)
            else:
                raise OSError("设备配置响应过长。")
        raise TimeoutError("配置提交响应超时，请用 USB 重试。")

    def upload_sleep_images(self, images, replace=False):
        """Write converted (remote, data) sleep pictures over USB, like provisioning."""
        if not self.usb or not self.info.get("device"):
            raise OSError("请连接 USB，并等待设备身份确认。")
        client = TfClient(self.usb, timeout=10)
        client.request("abort")
        client.mount()
        client.request("mkdir", path=SLEEP_FOLDER)
        if replace:
            for entry in list(client.list(SLEEP_FOLDER)):
                if not entry["dir"] and entry["name"].lower().endswith(".bmp"):
                    client.request("remove", path=f"{SLEEP_FOLDER}/{entry['name']}")
        # The upstream YMODEM sender accepts file paths. Only converted picture
        # bytes use this temporary file; WiFi provisioning stays in memory.
        with tempfile.TemporaryDirectory(prefix="xte-link-images-") as directory:
            local = Path(directory) / "sleep.bmp"
            for remote, data in images:
                local.write_bytes(data)
                client.upload(local, remote)
        self.hello_at = 0

    def close(self):
        for connection in (self.usb, self.tcp, self.discovery):
            if connection:
                connection.close()
        self.usb = self.tcp = self.discovery = None
