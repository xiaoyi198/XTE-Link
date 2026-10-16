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
import queue

from PIL import Image, ImageDraw
import pystray
from serial.tools import list_ports
from pc_driver import driver
from pc_driver.network import load_config, save_config, wifi_config
from pc_driver import sleep_image


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
        self.config = load_config(self.config_path)
        self.selected_port = self.config["port"]
        self.devices = {}
        self.usb_device = ""
        self.prompted = set()
        self.dialog_lock = threading.Lock()
        self.commands = queue.Queue(maxsize=1)
        self.status = "正在启动"
        self.stop_event = threading.Event()
        self.session_stop = threading.Event()
        self.lock = threading.Lock()
        self.icon = pystray.Icon("XTE-Link", icon_image(), "XTE-Link · 正在启动", pystray.Menu(
            pystray.MenuItem(lambda item: self.status, None, enabled=False),
            pystray.MenuItem("串口", pystray.Menu(self.port_items)),
            pystray.MenuItem("连接方式", pystray.Menu(
                pystray.MenuItem("自动跟随设备", self.select_mode("auto"), checked=lambda item: self.config["mode"] == "auto", radio=True),
                pystray.MenuItem("串口直连", self.select_mode("usb"), checked=lambda item: self.config["mode"] == "usb", radio=True),
                pystray.MenuItem("局域网连接", self.select_mode("lan"), checked=lambda item: self.config["mode"] == "lan", radio=True))),
            pystray.MenuItem("设备", pystray.Menu(self.device_items)),
            pystray.MenuItem("配置 WiFi（USB）", lambda icon, item: self.configure_wifi(icon, item)),
            pystray.MenuItem("上传休眠图片（USB）", self.sleep_images_dialog),
            pystray.MenuItem("驱动设置", self.settings_dialog),
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
            try:
                self.save_options(port=port)
            except OSError:
                logging.exception("Cannot save tray port")
            self.set_status("正在切换串口")
        return select

    def save_options(self, **values):
        with self.lock:
            self.config = save_config(self.config_path, **values)
            self.selected_port = self.config["port"]
            self.session_stop.set()

    def select_mode(self, mode):
        def select(icon, item):
            try:
                self.save_options(mode=mode)
                self.set_status("正在切换连接方式")
            except OSError:
                logging.exception("Cannot save connection mode")
        return select

    def device_items(self):
        devices = {**self.config["devices"], **self.devices}
        if not devices:
            yield pystray.MenuItem("请先连接 USB 配置", None, enabled=False)
        for device in sorted(devices):
            yield pystray.MenuItem(device, self.select_device(device),
                                  checked=lambda item, device=device: self.config["device"] == device, radio=True)

    def select_device(self, device):
        def choose(icon, item):
            try:
                self.save_options(device=device)
                self.set_status("正在切换设备")
            except OSError:
                logging.exception("Cannot save device selection")
        return choose

    def device_changed(self, info):
        device = info["device"]
        self.devices[device] = dict(info)
        self.usb_device = device
        self.config = load_config(self.config_path)
        self.icon.update_menu()
        if (not info["configured"] or device not in self.config["devices"]) and device not in self.prompted:
            self.prompted.add(device)
            self.configure_wifi(None, None, device)

    def dialog(self, build):
        if not self.dialog_lock.acquire(blocking=False):
            return
        def show():
            try:
                import tkinter as tk
                from tkinter import ttk, messagebox
                window = tk.Tk()
                window.title("XTE-Link")
                window.resizable(False, False)
                window.attributes("-topmost", True)
                build(window, tk, ttk, messagebox)
                window.mainloop()
            except Exception:
                logging.exception("Cannot open settings window")
            finally:
                self.dialog_lock.release()
        threading.Thread(target=show, name="xte-settings", daemon=True).start()

    def configure_wifi(self, icon, item, device=None):
        def build(window, tk, ttk, messagebox):
            candidates = list(self.devices)
            target = device or self.usb_device or (candidates[0] if len(candidates) == 1 else "")
            ttk.Label(window, text=f"通过 USB 配置设备 {target or '（等待设备连接）'}", padding=12).grid(row=0, column=0, columnspan=2)
            ttk.Label(window, text="WiFi 名称（2.4 GHz）").grid(row=1, column=0, padx=12, pady=6)
            ssid = ttk.Entry(window, width=32)
            ssid.grid(row=1, column=1, padx=12, pady=6)
            ttk.Label(window, text="密码").grid(row=2, column=0, padx=12, pady=6)
            password = ttk.Entry(window, width=32, show="●")
            password.grid(row=2, column=1, padx=12, pady=6)
            ttk.Label(window, text="配置以明文保存在 TF 卡；写入后请在设备设置切到局域网。", padding=12).grid(row=3, column=0, columnspan=2)
            results = queue.Queue()
            progress = ttk.Label(window, text="", padding=12)
            progress.grid(row=4, column=0, columnspan=2)
            def submit():
                try:
                    wifi_config(target or "", "0" * 64, ssid.get(), password.get())
                    name, secret = ssid.get(), password.get()
                    self.commands.put_nowait((lambda session: session.provision(target, name, secret), results.put))
                except (ValueError, queue.Full):
                    messagebox.showerror("无法提交", "请确认 USB 设备、WiFi 名称和密码（8～63 个字符），并等待上次配置完成。", parent=window)
                    return
                button.configure(state="disabled")
                progress.configure(text="正在写入并校验，请保持 USB 连接……")
            button = ttk.Button(window, text="写入设备", command=submit)
            button.grid(row=5, column=0, columnspan=2, pady=12)
            def poll():
                try:
                    error = results.get_nowait()
                except queue.Empty:
                    window.after(100, poll)
                    return
                button.configure(state="normal")
                password.delete(0, "end")
                if error:
                    progress.configure(text="配置未完成，可以重试。")
                    messagebox.showerror("配置失败", error, parent=window)
                    window.after(100, poll)
                else:
                    self.config = load_config(self.config_path)
                    messagebox.showinfo("配置已保存", "请在设备设置中选择“局域网连接”。驱动会自动跟随。", parent=window)
                    window.destroy()
            window.after(100, poll)
            ssid.focus_set()
        self.dialog(build)

    def sleep_images_dialog(self, icon, item):
        def build(window, tk, ttk, messagebox):
            from tkinter import filedialog
            from PIL import ImageTk
            view = (600, 360)
            entries = []
            state = {"index": 0, "drag": None, "job": None, "photo": None}
            ttk.Label(window, text="上传到 TF 卡 /.sleep/，每次休眠随机显示一张；TF 根目录有 /sleep.bmp 时只显示它。\n"
                                   "拖动预览调整取景，滚轮缩放；停下后显示设备上的 4 级灰度效果。",
                      padding=12).grid(row=0, column=0, columnspan=4)
            canvas = tk.Canvas(window, width=view[0], height=view[1], background="white", highlightthickness=1)
            canvas.grid(row=1, column=0, columnspan=4, padx=12)
            title = ttk.Label(window, text="尚未选择图片")
            title.grid(row=2, column=1, columnspan=2, pady=6)
            replace = tk.BooleanVar(value=False)
            ttk.Checkbutton(window, text="先删除设备上已有的休眠图片", variable=replace).grid(row=3, column=0, columnspan=4)
            progress = ttk.Label(window, text="", padding=6)
            progress.grid(row=5, column=0, columnspan=4)
            results = queue.Queue()

            def show(exact=True):
                if state["job"]:
                    window.after_cancel(state["job"])
                    state["job"] = None
                canvas.delete("all")
                if not entries:
                    return
                entry = entries[state["index"]]
                if exact:
                    shown = sleep_image.render(entry["image"], entry["zoom"], entry["center"]).convert("L")
                    shown = shown.resize(view, Image.Resampling.LANCZOS)
                else:  # Fast, undithered frame while dragging or zooming.
                    box = sleep_image.crop_box(entry["thumb"].size, entry["zoom"], entry["center"])
                    shown = entry["thumb"].resize(view, Image.Resampling.BILINEAR, box=box).convert("L")
                state["photo"] = ImageTk.PhotoImage(shown, master=window)
                canvas.create_image(0, 0, anchor="nw", image=state["photo"])
                title.configure(text=f"{state['index'] + 1}/{len(entries)}  {Path(entry['path']).name}  "
                                     f"缩放 {entry['zoom']:.1f}×")

            def adjust():
                show(exact=False)
                state["job"] = window.after(300, show)

            def press(event):
                state["drag"] = (event.x, event.y)

            def motion(event):
                if not entries or not state["drag"]:
                    return
                entry = entries[state["index"]]
                width, height = entry["image"].size
                left, top, right, bottom = sleep_image.crop_box(entry["image"].size, entry["zoom"], entry["center"])
                # Dragging moves the picture, so the crop window moves the other way.
                dx = (event.x - state["drag"][0]) / view[0] * (right - left) / width
                dy = (event.y - state["drag"][1]) / view[1] * (bottom - top) / height
                entry["center"] = ((left + right) / 2 / width - dx, (top + bottom) / 2 / height - dy)
                state["drag"] = (event.x, event.y)
                adjust()

            def wheel(event):
                if not entries:
                    return
                entry = entries[state["index"]]
                factor = 1.1 if event.delta > 0 else 1 / 1.1
                entry["zoom"] = min(max(entry["zoom"] * factor, 1.0), sleep_image.MAX_ZOOM)
                adjust()

            def step(offset):
                if entries:
                    state["index"] = (state["index"] + offset) % len(entries)
                    show()

            def reset():
                if entries:
                    entries[state["index"]].update(zoom=1.0, center=(0.5, 0.5))
                    show()

            canvas.bind("<ButtonPress-1>", press)
            canvas.bind("<B1-Motion>", motion)
            canvas.bind("<MouseWheel>", wheel)
            ttk.Button(window, text="上一张", command=lambda: step(-1)).grid(row=2, column=0, pady=6)
            ttk.Button(window, text="下一张", command=lambda: step(1)).grid(row=2, column=3, pady=6)
            actions = ttk.Frame(window)
            actions.grid(row=4, column=0, columnspan=4, pady=6)

            def choose():
                paths = filedialog.askopenfilenames(parent=window, title="选择休眠图片", filetypes=[
                    ("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"), ("全部文件", "*.*")])
                if not paths:
                    return
                try:
                    chosen = []
                    for path in paths:
                        image = sleep_image.load(path)
                        thumb = image.copy()
                        thumb.thumbnail((1600, 1600))
                        chosen.append({"path": path, "image": image, "thumb": thumb, "zoom": 1.0, "center": (0.5, 0.5)})
                except (OSError, ValueError) as exc:
                    messagebox.showerror("无法读取图片", str(exc), parent=window)
                    return
                entries[:] = chosen
                state["index"] = 0
                upload.configure(state="normal", text=f"上传 {len(entries)} 张")
                show()

            def submit():
                images = []
                for entry in entries:
                    data = sleep_image.encode(sleep_image.render(entry["image"], entry["zoom"], entry["center"]))
                    images.append((sleep_image.remote_name(entry["path"], data), data))
                try:
                    self.commands.put_nowait((lambda session: session.upload_sleep_images(images, replace.get()), results.put))
                except queue.Full:
                    messagebox.showerror("无法提交", "上一项 USB 操作尚未完成，请稍后重试。", parent=window)
                    return
                upload.configure(state="disabled")
                progress.configure(text=f"正在上传 {len(images)} 张图片，每张约 10 秒，请保持 USB 连接……")

            ttk.Button(actions, text="选择图片…", command=choose).grid(row=0, column=0, padx=6)
            ttk.Button(actions, text="重置裁切", command=reset).grid(row=0, column=1, padx=6)
            upload = ttk.Button(actions, text="上传", command=submit, state="disabled")
            upload.grid(row=0, column=2, padx=6)

            def poll():
                try:
                    error = results.get_nowait()
                except queue.Empty:
                    window.after(100, poll)
                    return
                upload.configure(state="normal")
                if error:
                    progress.configure(text="上传未完成，可以重试。")
                    messagebox.showerror("上传失败", error, parent=window)
                    window.after(100, poll)
                else:
                    messagebox.showinfo("上传完成", "长按电源键休眠即可看到图片。", parent=window)
                    window.destroy()
            window.after(100, poll)
        self.dialog(build)

    def settings_dialog(self, icon, item):
        def build(window, tk, ttk, messagebox):
            config = load_config(self.config_path)
            ttk.Label(window, text="常规推送间隔（秒，5～3600）").grid(row=0, column=0, padx=12, pady=12)
            interval = ttk.Entry(window, width=28)
            interval.insert(0, f"{config['interval']:g}")
            interval.grid(row=0, column=1, padx=12, pady=12)
            ttk.Label(window, text="局域网 IP（留空自动发现）").grid(row=1, column=0, padx=12, pady=6)
            host = ttk.Entry(window, width=28)
            host.insert(0, config["host"])
            host.grid(row=1, column=1, padx=12, pady=6)
            ttk.Label(window, text="AI 事件立即发送；设备上的屏幕更新间隔独立生效。", padding=12).grid(row=2, column=0, columnspan=2)
            def submit():
                try:
                    seconds = float(interval.get())
                    if not 5 <= seconds <= 3600 or len(host.get().strip()) > 253:
                        raise ValueError()
                    self.save_options(interval=seconds, host=host.get().strip())
                except (ValueError, OSError):
                    messagebox.showerror("无法保存", "请输入 5～3600 秒的间隔和有效地址。", parent=window)
                    return
                window.destroy()
            ttk.Button(window, text="保存", command=submit).grid(row=3, column=0, columnspan=2, pady=12)
        self.dialog(build)

    def set_status(self, text):
        self.status = text
        self.icon.title = f"XTE-Link · {text}"
        self.icon.update_menu()

    def connection_changed(self, connected, port):
        self.set_status(f"已连接 {port}" if connected else str(port))

    def run_driver(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    if self.stop_event.is_set():
                        break
                    config = load_config(self.config_path)
                    port = config["port"]
                    session_stop = self.session_stop = threading.Event()
                self.set_status("正在检测串口和局域网设备")
                try:
                    args = ["--mode", config["mode"], "--interval", str(config["interval"]),
                            "--config", str(self.config_path)]
                    if port:
                        args += ["--port", port]
                    driver.main(args, stop_event=session_stop, on_connection=self.connection_changed,
                                on_device=self.device_changed, commands=self.commands)
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
