"""Build the Windows tray EXE and record its checksum and build dependencies."""

from datetime import datetime
import hashlib
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from pc_driver.tray import icon_image

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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
        "右键图标：查看连接状态、选择串口、打开日志、退出。\n"
        "默认自动检测单个 ESP32 USB-Serial/JTAG 串口，多设备时请手动选择。\n"
        "串口选择保存在用户目录 .xte-link/tray.json；日志在 .xte-link/driver.log。\n"
        "先关闭旧 Python 驱动或串口监视器，避免串口占用。\n"
        "重复双击仅保留一个托盘实例；程序不会设置开机自启。\n", encoding="utf-8")
    print(f"Built: {exe}\nSHA-256: {checksum}")


if __name__ == "__main__":
    main()
