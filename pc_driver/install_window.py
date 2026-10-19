"""Small Tk maintenance window; all device work stays in a single worker."""

import logging
from pathlib import Path
import queue
import threading
import time

from .install import Bundle, Installer


def build_window(app, window, tk, ttk, messagebox, maintenance=False):
    from tkinter import filedialog
    from serial.tools import list_ports
    window.title("XTE-Link · 连接与版本状态")
    bundle = Bundle()
    panel = ttk.Frame(window, padding=16)
    panel.pack(fill="both", expand=True)
    heading = ttk.Label(panel, text="XTE-Link 连接与版本状态", font=("Microsoft YaHei UI", 16))
    heading.grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(panel, text="初代 Xteink X4 · USB 连接 · 字库与图片需要可用 TF 卡", padding=(0, 8)).grid(row=1, column=0, columnspan=3, sticky="w")
    ports = ttk.Combobox(panel, width=50, state="readonly")
    ports.grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
    state = {"busy": False, "source": None, "last": 0.0, "maintenance": maintenance, "phase": "准备安装"}
    choices = {}
    identities = {}
    def refresh():
        choices.clear()
        identities.clear()
        for port in list_ports.comports():
            if (port.vid, port.pid) == (0x303A, 0x1001):
                choices[f"{port.device} · {port.description}"] = port.device
                mac = (port.serial_number or "").replace(":", "")
                try:
                    identities[port.device] = bytes.fromhex(mac)[::-1].hex() if len(mac) == 12 else ""
                except ValueError:
                    identities[port.device] = ""
        ports["values"] = list(choices)
        selected = next((label for label, value in choices.items() if value == app.state_snapshot()["port"]), None)
        ports.set(selected or (next(iter(choices)) if len(choices) == 1 else ""))
    refresh()
    refresh_button = ttk.Button(panel, text="重新检测", command=refresh)
    refresh_button.grid(row=2, column=2, padx=(10, 0))
    current = "连接后检查"
    versions = ttk.Label(panel, text=f"当前固件：{current}    配套固件：{bundle.manifest['version']}", padding=(0, 8))
    versions.grid(row=3, column=0, columnspan=3, sticky="w")
    actions = ttk.Frame(panel)
    actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)
    events = queue.Queue()
    cancel = app.installer_cancel
    buttons = []
    progress = ttk.Progressbar(panel, maximum=100, length=560)
    progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
    status = ttk.Label(panel, text=app.status, wraplength=570)
    status.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)
    log = tk.Text(panel, width=80, height=12, state="disabled", wrap="word")
    log.grid(row=7, column=0, columnspan=3, pady=8)
    log.tag_configure("error", foreground="#a82222")
    def append(text, error=False):
        log.configure(state="normal")
        log.insert("end", text + "\n", "error" if error else ())
        log.see("end")
        log.configure(state="disabled")
    def report(text, value):
        # YMODEM/font callbacks can be frequent. Bound the UI event queue rate.
        now = time.monotonic()
        if value in (0, 1) or now - state["last"] >= .15:
            state["last"] = now
            events.put(("progress", text, value))
    def start(action):
        port = choices.get(ports.get())
        if not port:
            messagebox.showerror("请选择设备", "请连接并唤醒 X4，重新检测 USB 串口。多设备时请选择目标。", parent=window)
            return
        if state["busy"] or not state["maintenance"]:
            return
        if action in ("all", "firmware"):
            identity = identities.get(port) or "无法从 USB 确认（安装时将再次核对）"
            scope = "应用 app1（具体地址与容量由设备分区表核验）"
            if action == "all":
                scope += "；TF 卡 /fonts/、内置 UI 素材和两张内置屏保"
            if not messagebox.askokcancel("确认安装目标与写入范围", f"目标串口：{port}\n设备 ID：{identity}\n"
                    f"配套固件：{bundle.manifest['version']}\n写入范围：{scope}\n\n"
                    "首次为该设备保存当前完整 Flash 备份；后续复用匹配的有效备份。\n"
                    "保留引导器、恢复 app0、分区、设置及用户图片。\n确认后开始准备和安装，请保持 USB 连接。", parent=window):
                return
        with app.lock:
            if app.stop_event.is_set():
                return
            state["busy"] = True
            state["phase"] = "准备安装"
            app.installer_running.set()
            cancel.clear()
        for button in buttons:
            button.configure(state="disabled")
        ports.configure(state="disabled")
        refresh_button.configure(state="disabled")
        stop.configure(state="normal")
        def worker():
            try:
                Installer(app.root, port, report, cancel, app.exclusive_device,
                          source_zip=state["source"], bundle=bundle).run(action)
                (app.root / "installer-seen").touch()
                events.put(("done", "安装完成。可以继续使用托盘驱动。", 1))
            except InterruptedError as exc:
                events.put(("stopped", str(exc), 0))
            except Exception as exc:
                logging.exception("Device installation failed")
                events.put(("error", str(exc), 0))
            finally:
                app.installer_running.clear()
        # Device writes must finish their safe boundary before process shutdown.
        threading.Thread(target=worker, name="xte-installer", daemon=False).start()
    for index, (title, action) in enumerate((("一键完整安装／更新", "all"), ("刷入配套固件", "firmware"),
                                            ("下载构建并安装字库", "fonts"), ("安装内置图片素材", "assets"))):
        button = ttk.Button(actions, text=title, command=lambda action=action: start(action))
        button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 8), pady=4)
        buttons.append(button)
    actions.columnconfigure((0, 1), weight=1)
    def import_zip():
        selected = filedialog.askopenfilename(parent=window, title="导入官方 MiSans.zip（可选，避免重复下载）", filetypes=[("官方字体包", "*.zip")])
        if selected:
            state["source"] = selected
            append("使用本地官方字体包：" + selected)
    source_button = ttk.Button(panel, text="导入官方字体包…", command=import_zip)
    source_button.grid(row=8, column=0, sticky="w")
    buttons.append(source_button)
    def about():
        import os
        destination = app.root / "about"
        destination.mkdir(exist_ok=True)
        import shutil
        for name in ("使用说明.txt", "THIRD-PARTY-NOTICES.txt", "corresponding-source.zip"):
            shutil.copyfile(bundle.path / name, destination / name)
        os.startfile(destination)
    ttk.Button(panel, text="说明、许可与源码", command=about).grid(row=8, column=1)
    def request_stop():
        cancel.set()
        status.configure(text=f"正在停止（{state['phase']}）：下载与候选上传在下一安全点取消；文件切换／固件写入先完成必要校验。")
        stop.configure(state="disabled")
    stop = ttk.Button(panel, text="停止", command=request_stop, state="disabled")
    stop.grid(row=8, column=2, sticky="e")
    notice = ttk.Label(panel, text="首次刷机自动备份当前系统；更新保留恢复区、设置及用户图片。\n"
              "只支持已验证分区布局。XTE-Link 使用小米提供的 MiSans 字体。", padding=(0, 10))
    notice.grid(row=9, column=0, columnspan=3, sticky="w")
    maintenance_widgets = (actions, progress, log, source_button, stop, notice)
    def enter_maintenance():
        state["maintenance"] = True
        app.open_maintenance.clear()
        window.title("XTE-Link · 设备安装与维护")
        heading.configure(text="XTE-Link 设备安装与维护")
        for widget in maintenance_widgets:
            widget.grid()
        maintenance_button.grid_remove()
        status.configure(text="准备就绪。首次下载 MiSans 官方字体包约 228 MB；生成后自动缓存。")
    maintenance_button = ttk.Button(panel, text="设备安装与维护…", command=enter_maintenance)
    maintenance_button.grid(row=4, column=0, columnspan=3, pady=8)
    if maintenance:
        enter_maintenance()
    else:
        for widget in maintenance_widgets:
            widget.grid_remove()
    def close():
        if state["busy"]:
            request_stop()
        else:
            (app.root / "installer-seen").touch()
            window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    def poll():
        selected = identities.get(choices.get(ports.get()), "")
        current = app.state_snapshot()["devices"].get(selected, {}).get("firmware", "连接后检查")
        versions.configure(text=f"当前固件：{current}    配套固件：{bundle.manifest['version']}")
        if app.open_maintenance.is_set() and not state["maintenance"]:
            enter_maintenance()
        if not state["maintenance"]:
            status.configure(text=app.status)
        elif state["busy"] and cancel.is_set():
            request_stop()
        previous = None
        while True:
            try:
                kind, text, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                state["phase"] = text
            status.configure(text=text)
            progress["value"] = value * 100
            # Keep per-file/stage results, without filling the log per 1 KiB packet.
            if kind != "progress" or value in (0, 1):
                if text != previous:
                    append(text, kind == "error")
                    previous = text
            if kind in ("done", "error", "stopped"):
                state["busy"] = False
                for button in buttons:
                    button.configure(state="normal")
                ports.configure(state="readonly")
                refresh_button.configure(state="normal")
                stop.configure(state="disabled")
        if app.stop_event.is_set() and not state["busy"]:
            window.destroy()
            return
        window.after(100, poll)
    window.after(100, poll)
