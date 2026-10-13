"""Windows tray entry point; reuse the existing telemetry driver."""

import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading

from PIL import Image, ImageDraw
import pystray
from serial.tools import list_ports
from pc_driver import driver


def icon_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill="#163b48")
    draw.rounded_rectangle((12, 13, 52, 43), radius=3, fill="#edf3df")
    draw.line((17, 34, 24, 34, 28, 22, 34, 37, 39, 29, 47, 29), fill="#163b48", width=3)
    draw.line((24, 51, 40, 51), fill="#edf3df", width=4)
    return image


def automatic_port(ports):
    # Verified USB-Serial/JTAG identity; do not open unrelated serial devices.
    candidates = [p.device for p in ports if (p.vid, p.pid) == (0x303A, 0x1001)]
    return candidates[0] if len(candidates) == 1 else None


class LogStream:
    def write(self, text):
        if text.strip():
            logging.info("%s", text.rstrip())
        return len(text)

    def flush(self):
        for handler in logging.getLogger().handlers:
            handler.flush()


class TrayApp:
    def __init__(self, root):
        self.root = root
        self.config_path = root / "tray.json"
        self.log_path = root / "driver.log"
        self.selected_port = None
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8")).get("port")
            if isinstance(value, str) and value.upper().startswith("COM") and value[3:].isdigit():
                self.selected_port = value.upper()
        except (OSError, ValueError, AttributeError):
            pass
        self.status = "正在启动"
        self.stop_event = threading.Event()
        self.session_stop = threading.Event()
        self.lock = threading.Lock()
        self.icon = pystray.Icon("XTE-Link", icon_image(), "XTE-Link · 正在启动", pystray.Menu(
            pystray.MenuItem(lambda item: self.status, None, enabled=False),
            pystray.MenuItem("串口", pystray.Menu(self.port_items)),
            pystray.MenuItem("打开日志", self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit),
        ))
        self.worker = threading.Thread(target=self.run_driver, name="xte-telemetry")

    def port_items(self):
        yield pystray.MenuItem("自动检测", self.select_port(None),
                               checked=lambda item: self.selected_port is None, radio=True)
        devices = {p.device: p.description for p in list_ports.comports()}
        if self.selected_port and self.selected_port not in devices:
            devices[self.selected_port] = "未连接"
        for port, description in sorted(devices.items()):
            yield pystray.MenuItem(f"{port} · {description}", self.select_port(port),
                                   checked=lambda item, port=port: self.selected_port == port,
                                   radio=True)

    def select_port(self, port):
        def select(icon, item):
            with self.lock:
                self.selected_port = port
                self.session_stop.set()
            try:
                self.config_path.write_text(json.dumps({"port": port}), encoding="utf-8")
            except OSError:
                logging.exception("Cannot save tray port")
            self.set_status("正在切换串口")
        return select

    def set_status(self, text):
        self.status = text
        self.icon.title = f"XTE-Link · {text}"
        self.icon.update_menu()

    def connection_changed(self, connected, port):
        self.set_status(f"已连接 {port}" if connected else f"等待 {port}（未连接或被占用）")

    def run_driver(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    if self.stop_event.is_set():
                        break
                    port = self.selected_port or automatic_port(list_ports.comports())
                    session_stop = self.session_stop = threading.Event()
                if port is None:
                    self.set_status("等待设备（可在串口菜单选择）")
                    session_stop.wait(1)
                    continue
                self.set_status(f"正在连接 {port}")
                try:
                    driver.main(["--port", port], stop_event=session_stop,
                                on_connection=self.connection_changed)
                except Exception:
                    logging.exception("Telemetry worker failed")
                    self.set_status("运行异常，请查看日志")
                    session_stop.wait(2)
        except Exception:
            logging.exception("Tray worker failed")
            self.set_status("运行异常，请查看日志")
        finally:
            comtypes.CoUninitialize()

    def open_log(self, icon, item):
        os.startfile(self.log_path)

    def quit(self, icon, item):
        self.stop_event.set()
        with self.lock:
            self.session_stop.set()
        icon.stop()

    def setup(self, icon):
        icon.visible = True
        logging.info("Tray icon visible; telemetry starting")
        self.worker.start()

    def run(self):
        try:
            self.icon.run(self.setup)
        finally:
            self.stop_event.set()
            with self.lock:
                self.session_stop.set()
            if self.worker.ident is not None:
                self.worker.join()
            logging.info("Tray stopped; serial port released")


def main():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel32.CreateMutexW(None, False, "Local\\XTE-Link-Tray")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(mutex)
        return 0
    try:
        root = Path(os.environ.get("USERPROFILE", Path.home())) / ".xte-link"
        root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(root / "driver.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(message)s", force=True)
        sys.stdout = sys.stderr = LogStream()
        logging.info("XTE-Link tray started")
        TrayApp(root).run()
        return 0
    except Exception:
        logging.exception("XTE-Link startup failed")
        ctypes.windll.user32.MessageBoxW(None, "启动失败，请查看用户目录 .xte-link/driver.log。",
                                       "XTE-Link", 0x10)
        return 1
    finally:
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
