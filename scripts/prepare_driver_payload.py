"""Build a checked, font-free single-EXE payload from a specific app release."""

from datetime import datetime, timezone
from importlib.metadata import distribution
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


PARSER_SHA = "ddf6a05b170f9014fe7046b1b244dd4b3ef41254d926fd4e7f7b94ee300531e2"

GUIDE = """XTE-Link 单文件驱动与安装工具

双击 EXE，首次自动打开“设备安装与维护”；以后从托盘打开。
USB 连接并唤醒初代 Xteink X4，插入可用 TF 卡，选择唯一或指定的 USB 设备。
一键完整安装／更新：先准备 MiSans 字库，再备份／刷固件、写字库与内置图片，最后校验。
也可单独刷入固件、安装字库或安装素材。程序不需要外部 Python、PlatformIO 或 esptool。
首次字库下载约 228 MB。XTE-Link 使用小米提供的 MiSans 字体；官方许可在本文件附带的许可中。
字体在本机生成，不随 EXE 分发 ZIP、TTF 或 EPDF。可导入自己从官方下载的 MiSans.zip。
缓存、完整 Flash 及更新前应用备份、日志位于用户目录 .xte-link/。
首次接触设备保存当前完整 Flash；若已刷过第三方系统，此备份不能称为原厂备份。
刷写前验证芯片、容量、引导器、真实分区表、OTA CRC/状态、恢复应用及固件摘要。
目前只支持已验证的 Pixel 3.0.12 恢复布局及沿用该布局的 XTE-Link；未知原厂／其他布局拒绝刷写。
不写引导器、分区表、NVS、otadata 或恢复 app0；仅写已确认启动的应用 app1。
没有完成固件校验和重启握手时不报告成功。刷写期间请保持 USB 和电源连接。
中断刷写可能使应用暂时不能启动，可重新通过 USB 点击刷入配套固件；已有备份与恢复槽保留。
TF 安装复用 YMODEM-1K 和完整卡内回读校验；同内容跳过。候选文件校验后保留旧文件并切换。
不格式化卡，不删除用户图片、旧 Zheng 字库、WiFi 或 NVS 设置。
停止将在当前文件传输或固件校验结束后生效。意外断电后重新执行可恢复常见替换中断。
设置 WiFi、休眠图片上传、状态推送、Codex 审批、Clash 功能仍从托盘使用。
复杂审批在电脑处理。程序不会设置开机自启。

本程序包含 GPL-2.0-or-later 的 esptool。许可与对应源码可以在“说明、许可与源码”导出。
对应源码中保留项目 MIT 许可与第三方各自许可；esptool 来源：https://github.com/espressif/esptool/tree/v4.9.0
分区工具原样取自固定 Arduino-ESP32 包，Apache-2.0，来源与摘要见安装清单。
"""


def prepare(root, firmware_dir):
    root, firmware_dir = Path(root), Path(firmware_dir)
    original = json.loads((firmware_dir / "manifest.json").read_text(encoding="utf-8"))
    image = firmware_dir / "firmware.bin"
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest(image) != original["sha256"] or image.stat().st_size != original["bytes"]:
        raise ValueError("Firmware archive checksum mismatch")
    from pc_driver.flash import validate_image
    validate_image(image.read_bytes())
    for name, expected in original["source_hashes"].items():
        if digest(root / name) != expected:
            raise ValueError(f"Firmware does not match current sources: {name}; rebuild and archive first")
    stage = root / "build" / "pc-driver" / "install_payload"
    stage.mkdir(parents=True, exist_ok=True)
    assets, support = [], []
    def copy(source, relative, target_list, **fields):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        info = {"file": relative, "bytes": destination.stat().st_size, "sha256": digest(destination), **fields}
        target_list.append(info)
        return info
    firmware = copy(image, "firmware.bin", [])
    copy(firmware_dir / "manifest.json", "firmware-manifest.json", support)
    copy(root / "tools/misans/reference-manifest.json", "font-reference.json", support)
    copy(root / "tools/misans/LICENSE-MiSans.txt", "LICENSE-MiSans.txt", support)
    ui_root = root / "assets/ui/gray-flat-v1"
    ui = json.loads((ui_root / "manifest.json").read_text(encoding="utf-8"))
    for item in ui["assets"]:
        if digest(ui_root / item["bmp"]) != item["sha256"]:
            raise ValueError("UI asset checksum mismatch")
        copy(ui_root / item["bmp"], "assets/ui/" + Path(item["bmp"]).name, assets, tf_path=item["tf_path"])
    skins_root = root / "assets/sleep-skins"
    for item in json.loads((skins_root / "manifest.json").read_text(encoding="utf-8"))["skins"]:
        if digest(skins_root / item["bmp"]) != item["sha256"]["bmp"]:
            raise ValueError("Sleep asset checksum mismatch")
        copy(skins_root / item["bmp"], "assets/sleep/" + item["bmp"], assets, tf_path="/.sleep/" + item["bmp"])
    parser = root / ".platformio/packages/framework-arduinoespressif32/tools/gen_esp32part.py"
    if digest(parser) != PARSER_SHA:
        raise ValueError("Pinned Espressif partition parser changed")
    copy(parser, "gen_esp32part.py", support)
    (stage / "使用说明.txt").write_text(GUIDE, encoding="utf-8")
    notices = [GUIDE, "\nProject license:\n", (root / "LICENSE").read_text(encoding="utf-8"),
               "\nCodex IPC adapter / codex-mobile:\n", (root / "pc_driver/LICENSE-codex-mobile").read_text(encoding="utf-8"),
               "\nOpenX4 Community SDK:\n", (root / "open-x4-sdk/LICENSE").read_text(encoding="utf-8"),
               "\nLoBo YMODEM receiver (source header):\n",
               (root / "third_party/esp32-ymodem/components/ymodem/ymodem.c").read_text(encoding="utf-8").split("*/", 1)[0] + "*/",
               "\nMiSans:\n", (root / "tools/misans/LICENSE-MiSans.txt").read_text(encoding="utf-8"),
               "\nPhosphor Icons 2.1.1:\n", (ui_root / "sources/phosphor/LICENSE").read_text(encoding="utf-8")]
    packages = ("esptool", "ymodem", "fonttools", "Pillow", "pystray", "pyserial", "psutil", "pycaw", "comtypes",
                "zeroconf", "ifaddr", "PyYAML", "ordered-set", "bitstring", "bitarray", "cryptography", "ecdsa",
                "reedsolo", "intelhex", "cffi", "pycparser", "six")
    source_archive = stage / "corresponding-source.zip"
    with zipfile.ZipFile(source_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        # Explicit source directories only: never include user settings, Flash or font binaries.
        for folder, patterns in (("pc_driver", ("*.py", "*.json", "*.txt", "*.md", "LICENSE*")), ("scripts", ("*.py",)),
                                  ("tools/misans", ("*.py", "*.json", "*.txt", "*.md")),
                                  ("firmware/src", ("*.h", "*.cpp")), ("patches", ("*.patch",))):
            for pattern in patterns:
                for path in (root / folder).glob(pattern):
                    archive.write(path, str(path.relative_to(root)))
        for name in ("LICENSE", "README.md", "platformio.ini", "requirements-dev.txt", ".gitmodules"):
            archive.write(root / name, name)
        archive.write(parser, "third_party/gen_esp32part.py")
        for name in packages:
            package = distribution(name)
            notices.append(f"\n{name} {package.version}\n")
            for file in package.files or []:
                lower = str(file).lower()
                local = package.locate_file(file)
                if any(part.startswith(("license", "copying", "notice")) for part in Path(lower).parts):
                    if local.is_file():
                        notices.append(local.read_text(encoding="utf-8", errors="replace"))
                        archive.write(local, "dependencies/" + name + "/" + str(file).replace("../", ""))
                # esptool's corresponding source is preserved as .py, including stub sources/data.
                if name == "esptool" and str(file).startswith(("esptool/", "espsecure/", "espefuse/")) and not lower.endswith(".pyc") and "dependencies/esptool/" + str(file) not in archive.namelist():
                    archive.write(local, "dependencies/esptool/" + str(file))
        archive.writestr("BUILD.txt", "Python 3.11; pip install -r pc_driver/requirements-build.txt; "
                         "initialize fixed SDK/YMODEM submodules and apply patches; "
                         "build firmware with PlatformIO; python scripts/build_driver.py --firmware <archived app directory>. "
                         "See BUILD-ORIGIN.json for fixed commits, source hashes and parser checksum. Dependencies retain upstream versions; SDK/YMODEM patches are included.\n")
        archive.writestr("BUILD-ORIGIN.json", json.dumps({"firmware": original, "parser_sha256": PARSER_SHA,
            "packages": {name: distribution(name).version for name in packages}}, ensure_ascii=False, indent=2))
        archive.writestr("THIRD-PARTY-NOTICES.txt", "\n".join(notices))
    (stage / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")
    for name in ("使用说明.txt", "THIRD-PARTY-NOTICES.txt", "corresponding-source.zip"):
        support.append({"file": name, "bytes": (stage / name).stat().st_size, "sha256": digest(stage / name)})
    version = next(line.split('"')[1] for line in (root / "firmware/src/main.cpp").read_text(encoding="utf-8").splitlines()
                   if line.startswith("constexpr const char* FIRMWARE_VERSION ="))
    manifest = {"schema": 1, "version": version, "firmware": firmware, "assets": assets, "support": support,
                "devices": json.loads((root / "pc_driver/device_profiles.json").read_text(encoding="utf-8")),
                "parser_source": "Arduino-ESP32 2.0.17, framework package 3.20017.241212+sha.dcc1105b; Apache-2.0",
                "sdk_commit": original["sdk_commit"], "ymodem_commit": original["ymodem_commit"]}
    (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return stage
