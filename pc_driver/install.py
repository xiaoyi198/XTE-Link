"""Bundled firmware/resources and local MiSans generation for the single EXE."""

from datetime import datetime, timezone
from io import BytesIO
import json
import logging
import math
import re
from pathlib import Path, PurePosixPath
import sys
import time
import urllib.request
import zipfile

from .flash import flash_device, sha256
from .tf import TfClient, remote_path


def payload_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "build" / "pc-driver")) / "install_payload"


class Bundle:
    def __init__(self, path=None):
        self.path = Path(path) if path else payload_root()
        self.manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema") != 1:
            raise ValueError("安装资源清单版本不兼容。")
        for item in [self.manifest["firmware"], *self.manifest["assets"], *self.manifest["support"]]:
            self.file(item)

    def file(self, item):
        relative = PurePosixPath(item["file"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in item["file"] or ":" in item["file"]:
            raise ValueError("安装资源路径非法。")
        path = (self.path / Path(*relative.parts)).resolve()
        if not path.is_relative_to(self.path.resolve()):
            raise ValueError("安装资源越界。")
        if path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise ValueError(f"安装资源损坏：{item['file']}")
        return path


def prepare_fonts(root, bundle, report, check_cancel, source_zip=None):
    from tools.misans import build_misans as converter
    from fontTools.ttLib import TTFont

    reference = json.loads((bundle.path / "font-reference.json").read_text(encoding="utf-8"))
    folder = Path(root) / "cache" / "misans-4.009-medium-v1"
    files = reference["files"]
    if all((folder / "fonts" / f["file"]).is_file()
           and (folder / "fonts" / f["file"]).stat().st_size == f["bytes"]
           and sha256(folder / "fonts" / f["file"]) == f["sha256"] for f in files):
        report("复用已校验的本地字库缓存", 1)
        return [(folder / "fonts" / f["file"], f) for f in files]
    source = Path(source_zip) if source_zip else Path(root) / "cache" / "MiSans.zip"
    if not source.is_file():
        if source_zip:
            raise FileNotFoundError("所选官方字体包不存在。")
        source.parent.mkdir(parents=True, exist_ok=True)
        part = source.with_suffix(".zip.part")
        report("从小米官方下载字体包（约 228 MB）……", 0)
        try:
            with urllib.request.urlopen(converter.URL, timeout=30) as response, part.open("wb") as output:
                done = 0
                while data := response.read(1024 * 1024):
                    check_cancel()
                    output.write(data)
                    done += len(data)
                    if done > reference["source_zip_bytes"]:
                        raise ValueError("官方字体包大小已变化，需核对版本后更新驱动。")
                    report(f"下载官方字体包 {done / 1e6:.1f} / {reference['source_zip_bytes'] / 1e6:.1f} MB",
                           done / reference["source_zip_bytes"])
            if part.stat().st_size != reference["source_zip_bytes"] or sha256(part) != converter.ZIP_SHA:
                raise ValueError("官方字体包 SHA-256 不匹配，未使用下载内容。")
            part.replace(source)
        finally:
            if part.exists():
                part.unlink()
    if sha256(source) != converter.ZIP_SHA:
        raise ValueError(f"字体包摘要不匹配，请重新导入官方 MiSans.zip：{source}")
    check_cancel()
    with zipfile.ZipFile(source) as archive:
        ttf = archive.read("MiSans/ttf/MiSans-Medium.ttf")
    if converter.sha(ttf) != converter.SOURCE_SHA["Medium"]:
        raise ValueError("MiSans Medium 源字体校验失败。")
    font = TTFont(BytesIO(ttf))
    cps = sorted(cp for cp in font.getBestCmap() if 32 <= cp <= 0xffff and not 0xd800 <= cp <= 0xdfff)
    font.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    staging = folder.with_name(folder.name + "-" + stamp)
    (staging / "fonts").mkdir(parents=True)
    for size in (24, 32, 40, 64):
        def progress(done, total):
            check_cancel()
            report(f"本机生成 MiSans {size}px 字库 {done}/{total}", done / total)
        check_cancel()
        info = converter.build(ttf, cps if size != 64 else sorted(map(ord, converter.DIGITS)), size, staging, progress)
        expected = next(f for f in files if f["pixel_size"] == size)
        if info["bytes"] != expected["bytes"] or info["sha256"] != expected["sha256"]:
            raise ValueError("生成的字库与已验证版本不同，未写入设备。请检查内置栅格化运行库。")
    (staging / "manifest.json").write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf-8")
    if folder.exists():
        folder.rename(folder.with_name(folder.name + "-previous-" + stamp))
    staging.rename(folder)
    return [(folder / "fonts" / f["file"], f) for f in files]


def same_file(info, expected):
    return (info.get("exists") is True and info.get("dir") is False
            and info.get("size") == expected["bytes"] and info.get("sha256") == expected["sha256"])


def install_file(client, local, expected, report, check_cancel):
    """Two renames preserve the old file until the new final path is read back."""
    target = remote_path(expected["tf_path"])
    candidate, backup = target + ".xte-next", target + ".xte-old"
    remote_path(candidate + ".xte-part")
    def stat(path):
        value = client.request("stat", path=path)
        if type(value.get("exists")) is not bool:
            raise OSError("设备文件状态响应不完整。")
        if value["exists"] and (value.get("dir") is not False or type(value.get("size")) is not int
                                or not isinstance(value.get("sha256"), str)):
            raise OSError(f"文件状态不兼容或路径被目录占用：{path}")
        return value
    def remove(path):
        if stat(path)["exists"]:
            client.request("remove", path=path)
    check_cancel()
    current, saved = stat(target), stat(backup)
    if not current["exists"] and saved["exists"]:
        client.request("rename", path=backup, to=target)
        current = stat(target)
        saved = {"exists": False}
    if same_file(current, expected):
        remove(candidate)
        remove(candidate + ".xte-part")
        remove(backup)
        report(f"已校验，跳过 {target}", 1)
        return False
    if saved["exists"]:
        raise OSError(f"发现未完成替换的冲突，已保留两份文件：{target} 与 {backup}")
    staged = stat(candidate)
    if not same_file(staged, expected):
        remove(candidate)
        remove(candidate + ".xte-part")
        client.request("mkdir", path=str(PurePosixPath(target).parent))
        client.progress = lambda done, total: report(f"上传 {target}  {done}/{total} 字节", done / max(total, 1))
        try:
            client.upload(local, candidate)
        finally:
            client.progress = None
        if not same_file(stat(candidate), expected):
            raise OSError(f"TF 候选文件回读校验失败：{target}")
    check_cancel()
    if current["exists"]:
        client.request("rename", path=target, to=backup)
    # If power/USB is lost here, the next run restores backup before any upload.
    try:
        client.request("rename", path=candidate, to=target)
        if not same_file(stat(target), expected):
            raise OSError(f"TF 最终文件校验失败，旧文件保留于 {backup}")
    except OSError:
        # Roll back when the connection is still usable; otherwise leave enough
        # on-card state for the next run to recover, without replaying a command.
        try:
            if stat(backup)["exists"]:
                if stat(target)["exists"] and not stat(candidate)["exists"]:
                    client.request("rename", path=target, to=candidate)
                if not stat(target)["exists"]:
                    client.request("rename", path=backup, to=target)
        except OSError:
            pass
        raise
    remove(backup)
    report(f"安装并校验完成 {target}", 1)
    return True


def wait_device(port, expected_mac=None, expected_version=None, timeout=35):
    from serial.tools import list_ports
    from .driver import open_serial
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [p for p in list_ports.comports() if (p.vid, p.pid) == (0x303A, 0x1001)]
        if expected_mac:
            candidates = [p for p in candidates if (p.serial_number or "").replace(":", "").lower() == expected_mac]
        else:
            candidates = [p for p in candidates if p.device == port]
        if len(candidates) != 1:
            time.sleep(.2)
            continue
        connection = None
        try:
            connection = open_serial(candidates[0].device)
            buffer = bytearray()
            info, version = None, None
            next_hello = 0
            attempt_deadline = min(deadline, time.monotonic() + 5)
            while time.monotonic() < attempt_deadline:
                if time.monotonic() >= next_hello:
                    connection.write(b'{"v":2,"cmd":"hello"}\n')
                    next_hello = time.monotonic() + .4
                buffer.extend(connection.read(4096))
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer[:] = tail
                    if line.startswith(b"XTE: link "):
                        info = json.loads(line[10:])
                    if line.startswith(b"XTE: firmware "):
                        version = line[14:].decode("ascii").strip()
                if len(buffer) > 8192:
                    buffer.clear()
                if info and (not expected_version or version == expected_version):
                    device = info.get("device", "")
                    if expected_mac and device != bytes.fromhex(expected_mac)[::-1].hex():
                        raise OSError("重启后的设备身份不一致。")
                    return connection, candidates[0].device
                time.sleep(.01)
        except (OSError, ValueError):
            if connection:
                connection.close()
            time.sleep(.2)
        else:
            if connection:
                connection.close()
    raise TimeoutError("等待设备启动超时。请唤醒设备、检查 USB，或重新刷入配套固件。")


def device_identity(port):
    from serial.tools import list_ports
    matches = [p for p in list_ports.comports() if p.device == port and (p.vid, p.pid) == (0x303A, 0x1001)]
    if len(matches) != 1:
        raise OSError("请选择已连接的初代 X4 USB 串口。")
    mac = (matches[0].serial_number or "").replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise OSError("无法从 USB 确认设备身份，未执行安装。")
    return mac


class Installer:
    def __init__(self, root, port, report, cancel, exclusive, source_zip=None, bundle=None):
        self.root, self.port = Path(root), port
        self.report, self.cancel, self.exclusive = report, cancel, exclusive
        self.source_zip, self.bundle = source_zip, bundle

    def check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError("已在安全阶段停止。已完成的文件会在下次执行时校验并跳过。")

    def run(self, action):
        if action not in ("all", "firmware", "fonts", "assets"):
            raise ValueError("未知安装操作。")
        bundle = self.bundle or Bundle()
        selected_mac = device_identity(self.port)
        self.report(f"配套固件：{bundle.manifest['version']}", 0)
        files = []
        if action in ("all", "fonts"):
            files += prepare_fonts(self.root, bundle, self.report, self.check_cancel, self.source_zip)
        if action in ("all", "assets"):
            files += [(bundle.file(item), item) for item in bundle.manifest["assets"]]
        self.check_cancel()
        with self.exclusive():
            mac = selected_mac
            if action in ("all", "firmware"):
                mac = flash_device(self.port, bundle, self.root, self.report, selected_mac, self.check_cancel)
            self.check_cancel()
            connection, port = wait_device(self.port, mac, bundle.manifest["version"] if action in ("all", "firmware") else None)
            try:
                if files:
                    client = TfClient(connection, timeout=60)
                    client.request("abort")
                    client.mount()
                    if client.install_version != 1:
                        raise ValueError("请先点击“一键刷入配套固件”，再安装字库或素材。")
                    self.report("检查 TF 卡可用空间……", 0)
                    space = client.request("space")
                    cluster = space.get("cluster")
                    if type(cluster) is not int or cluster <= 0 or type(space.get("free")) is not int:
                        raise OSError("无法确认 TF 卡剩余空间。")
                    required = sum(math.ceil(item["bytes"] / cluster) * cluster for _, item in files) + 8 * cluster
                    if space["free"] < required:
                        # Matching files do not need room for another copy.
                        missing = [item for _, item in files if not same_file(client.request("stat", path=item["tf_path"]), item)]
                        required = (8 * cluster if missing else 0) + sum(math.ceil(item["bytes"] / cluster) * cluster
                            for item in missing if not same_file(client.request("stat", path=item["tf_path"] + ".xte-next"), item))
                    if space["free"] < required:
                        raise OSError(f"TF 卡空间不足：需要约 {required / 1e6:.1f} MB，可用 {space['free'] / 1e6:.1f} MB。")
                    for index, (local, item) in enumerate(files, 1):
                        self.report(f"资源 {index}/{len(files)}：{item['tf_path']}", 0)
                        install_file(client, local, item, self.report, self.check_cancel)
                    connection.write(b'{"v":2,"cmd":"resources_ready"}\n')
                    self.report("资源已完整校验，正在刷新设备……", 1)
                    deadline = time.monotonic() + 8
                    reply = bytearray()
                    while b"XTE: resources ready" not in reply and time.monotonic() < deadline:
                        reply.extend(connection.read(4096))
                        if len(reply) > 8192:
                            del reply[:-4096]
                        time.sleep(.01)
                    if b"XTE: resources ready" not in reply:
                        raise TimeoutError("文件已校验，但未收到设备刷新确认；可以重新执行以核对状态。")
                self.report(f"完成：设备 {port}，驱动通信即将恢复。", 1)
            finally:
                connection.close()


def self_test(output, source_zip=None):
    """Read-only device diagnostic for validating the frozen distribution."""
    import threading
    import traceback
    from PIL import features
    from .flash import validate_image, partition_parser
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        bundle = Bundle()
        validate_image(bundle.file(bundle.manifest["firmware"]).read_bytes())
        partition_parser(bundle.path / "gen_esp32part.py")
        import esptool
        from ymodem.Socket import ModemSocket
        import fontTools
        result.update(version=bundle.manifest["version"], assets=len(bundle.manifest["assets"]),
                      esptool=esptool.__version__, fonttools=fontTools.__version__, freetype=features.version("freetype2"))
        if source_zip:
            cache = output.parent / "self-test-cache"
            files = prepare_fonts(cache, bundle, lambda *args: None, lambda: None, source_zip)
            result["fonts"] = [{"file": item["file"], "sha256": sha256(path)} for path, item in files]
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
