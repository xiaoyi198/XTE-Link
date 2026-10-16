"""Build the Windows tray EXE and record its checksum and build dependencies."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from pc_driver.tray import icon_image

    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    output = root / "artifacts" / "pc-driver" / stamp
    output.mkdir(parents=True, exist_ok=False)
    work = root / "build" / "pc-driver"
    work.mkdir(parents=True, exist_ok=True)
    icon = work / "xte-link.ico"
    icon_image().save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "XTE-Link", "--noupx",
        "--icon", str(icon), "--paths", str(root),
        "--hidden-import", "pystray._win32", "--hidden-import", "serial.urlhandler.protocol_loop",
        "--collect-submodules", "zeroconf",
        "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work), str(root / "pc_driver" / "tray.py"),
    ], cwd=root, check=True)
    exe = output / "XTE-Link.exe"
    checksum = hashlib.sha256(exe.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{checksum}  {exe.name}\n", encoding="ascii")
    frozen = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (output / "build-requirements.txt").write_text(frozen, encoding="utf-8")
    (output / "使用说明.txt").write_text(
        "双击 XTE-Link.exe；程序在系统托盘运行，无需安装 Python。\n"
        "右键图标：连接方式、串口、设备、配置 WiFi、上传休眠图片、驱动设置、日志、退出。\n"
        "先安装 lan-push-20261014 或更新的兼容固件；首次用 USB 连接，窗口中填写 2.4 GHz WiFi。\n"
        "WiFi 配置以明文保存在 TF 卡 /xte-link/network.json；此电脑的配对密钥保存在 .xte-link/tray.json。\n"
        "在设备设置中手动选择串口直连或局域网连接；驱动默认自动跟随该选择。\n"
        "局域网通过 mDNS 自动发现；无法发现时在驱动设置填写 IP，多设备时在设备菜单选择。\n"
        "常规推送间隔可设 5～3600 秒，默认 5 秒；AI 事件立即发送，心跳每 5 秒发送。\n"
        "AI 即时推送复用 DSH 实时审批与 agents.json 桥接；Codex 完整状态需要桥接数据。\n"
        "休眠图片需 sleep-image-20261016 或更新固件；通过 USB 上传到 TF 卡 /.sleep/，每次休眠随机显示一张。\n"
        "连接信息保存在用户目录 .xte-link/tray.json；日志在 .xte-link/driver.log。\n"
        "先关闭旧 Python 驱动或串口监视器，避免串口占用。\n"
        "重复双击仅保留一个托盘实例；程序不会设置开机自启。\n", encoding="utf-8")
    print(f"Built: {exe}\nSHA-256: {checksum}")


if __name__ == "__main__":
    main()
