"""Application-only flashing with the pinned Espressif tools and recovery checks."""

from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import zlib


FLASH_SIZE = 16 * 1024 * 1024


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def partition_parser(path):
    spec = importlib.util.spec_from_file_location("xte_esp32part", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.offset_part_table = 0x8000
    return module


def inspect_layout(system, parser, profiles, image_size):
    """Only admit a tested bootloader/partition layout with app1 already selected."""
    if len(system) != 0x10000:
        raise ValueError("启动区域读取不完整，未刷写。")
    table_bytes = system[0x8000:0x9000]
    table = parser.PartitionTable.from_binary(table_bytes)
    table.verify()
    table.verify_size_fits(FLASH_SIZE)
    profile = next((p for p in profiles
                    if p["partition_sha256"] == hashlib.sha256(table_bytes).hexdigest()
                    and p["boot_sha256"] == hashlib.sha256(system[:0x8000]).hexdigest()), None)
    if profile is None:
        raise ValueError("此设备的引导器／分区布局尚未验证，已停止刷写并保留原系统。")
    apps = sorted((p for p in table if p.type == 0), key=lambda p: p.subtype)
    ota = next((p for p in table if p.type == 1 and p.subtype == 0), None)
    if len(apps) != 2 or [p.subtype for p in apps] != [0x10, 0x11] or ota is None:
        raise ValueError("需要已验证的双应用恢复布局，未刷写。")
    recovery, target = apps
    if recovery.offset < 0x10000 or ota.offset + ota.size > len(system) or ota.size != 0x2000:
        raise ValueError("恢复区或 OTA 区域不兼容，未刷写。")
    entries = []
    for offset in (ota.offset, ota.offset + 0x1000):
        record = system[offset:offset + 32]
        seq, state, crc = struct.unpack_from("<I20xII", record)
        # CRC definition from ESP-IDF v4.4.7 otatool.py, without writing otadata.
        if 0 < seq < 0xffffffff and crc == zlib.crc32(record[:4], 0xffffffff) & 0xffffffff:
            if state in (0, 1):
                raise ValueError("OTA 应用仍待确认，未刷写。请先完成现有系统的启动验证。")
            if state in (2, 0xffffffff):
                entries.append(seq)
    if not entries or (max(entries) - 1) % 2 != 1:
        raise ValueError("启动槽未确认选择应用 app1，未修改 OTA 数据。")
    if image_size <= 0 or (image_size + 4095) // 4096 * 4096 > target.size:
        raise ValueError("固件超出应用分区，未刷写。")
    return profile, recovery, target


def validate_image(data):
    from esptool.bin_image import ESP32C3FirmwareImage
    image = ESP32C3FirmwareImage(io.BytesIO(data))
    if image.chip_id != 5 or image.checksum != image.calculate_checksum():
        raise ValueError("ESP32-C3 固件镜像校验失败。")
    if not image.append_digest or image.stored_digest != image.calc_digest:
        raise ValueError("固件内嵌 SHA-256 校验失败。")
    return image


def flash_device(port, bundle, root, report, expected_mac=None, check_cancel=lambda: None):
    """Do not erase all, change the boot selection or write any system partition."""
    import esptool
    from esptool.cmds import detect_flash_size

    image_path = bundle.file(bundle.manifest["firmware"])
    image_data = image_path.read_bytes()
    validate_image(image_data)
    parser = partition_parser(bundle.path / "gen_esp32part.py")
    esp = None
    try:
        check_cancel()
        report("正在识别芯片并进入下载模式……", 0)
        esp = esptool.detect_chip(port=port, baud=115200, connect_mode="usb_reset")
        if esp.CHIP_NAME != "ESP32-C3" or esp.secure_download_mode:
            raise ValueError("仅支持未启用安全下载限制的初代 X4 / ESP32-C3。")
        if esp.get_secure_boot_enabled() or esp.get_flash_encryption_enabled():
            raise ValueError("设备启用了安全启动或 Flash 加密，已停止刷写。")
        mac = bytes(esp.read_mac()).hex()
        if expected_mac and mac != expected_mac:
            raise ValueError("设备身份在准备期间发生变化，未刷写。请重新选择目标。")
        report(f"设备 {mac}：检查 Flash、分区和恢复程序……", 0)
        esp = esp.run_stub()
        esp.change_baud(460800)
        if detect_flash_size(esp) != "16MB":
            raise ValueError("Flash 容量与初代 X4 不匹配，未刷写。")
        esp.flash_set_parameters(FLASH_SIZE)
        system = esp.read_flash(0, 0x10000)
        profile, recovery, target = inspect_layout(system, parser, bundle.manifest["devices"], len(image_data))
        recovery_md5 = esp.flash_md5sum(recovery.offset, recovery.size)
        if recovery_md5 != profile["recovery_md5"]:
            raise ValueError("恢复程序与已验证版本不匹配，未刷写。")
        device_root = Path(root) / "backups" / mac
        device_root.mkdir(parents=True, exist_ok=True)
        backup_record = device_root / "full-flash.json"
        valid_backup = False
        if backup_record.is_file():
            info = json.loads(backup_record.read_text(encoding="utf-8"))
            path = Path(info["path"])
            valid_backup = (info.get("device") == mac and path.is_file()
                            and path.stat().st_size == FLASH_SIZE and sha256(path) == info.get("sha256"))
            if valid_backup:
                with path.open("rb") as stream:
                    saved_system = stream.read(0x10000)
                    stream.seek(recovery.offset)
                    saved_recovery = stream.read(recovery.size)
                valid_backup = (saved_system[:0x9000] == system[:0x9000]
                                and hashlib.md5(saved_recovery).hexdigest() == recovery_md5)
        if not valid_backup:
            report("首次安装：正在备份当前完整 Flash，请保持 USB 连接……", 0)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            path = device_root / f"current-full-{stamp}.bin"
            full = esp.read_flash(0, FLASH_SIZE, lambda done, total: report("备份当前完整 Flash", done / total))
            if hashlib.md5(full).hexdigest() != esp.flash_md5sum(0, FLASH_SIZE):
                raise OSError("完整备份与设备摘要不一致，未刷写。")
            with path.open("xb") as stream:
                stream.write(full)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            info = {"device": mac, "path": str(path.resolve()), "sha256": sha256(path),
                    "kind": "current system; not necessarily factory firmware"}
            temporary = backup_record.with_suffix(".tmp")
            temporary.write_text(json.dumps(info, indent=2), encoding="utf-8")
            temporary.replace(backup_record)
            del full
        check_cancel()
        # Keep the exact previous application slot independently for a USB retry/rollback.
        old_md5 = esp.flash_md5sum(target.offset, target.size)
        old_path = device_root / f"app1-{old_md5}.bin"
        if not old_path.exists():
            report("保存更新前的应用分区……", 0)
            previous = esp.read_flash(target.offset, target.size,
                                      lambda done, total: report("保存更新前的应用分区", done / total))
            if hashlib.md5(previous).hexdigest() != old_md5:
                raise OSError("旧应用备份校验失败，未刷写。")
            old_path.write_bytes(previous)
        elif hashlib.md5(old_path.read_bytes()).hexdigest() != old_md5:
            raise OSError("已有应用备份损坏，未刷写。")
        record = {"device": mac, "port": port, "offset": target.offset, "size": target.size,
                  "old_md5": old_md5, "new_sha256": hashlib.sha256(image_data).hexdigest(),
                  "previous_app": str(old_path), "recovery_md5": recovery_md5, "verified": False}
        journal = device_root / f"update-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.json"
        journal.write_text(json.dumps(record, indent=2), encoding="utf-8")
        # Recheck immediately before the only destructive operation.
        if esp.read_flash(0, 0x10000) != system:
            raise OSError("启动区域在准备期间发生变化，未刷写。")
        check_cancel()
        report(f"正在刷入配套固件（应用分区 {target.offset:#x}），请勿断电……", 0)
        esptool.main(["--chip", "esp32c3", "--port", port, "--no-stub", "--baud", "460800", "--after", "no_reset_stub",
                      "write_flash", "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep",
                      hex(target.offset), str(image_path)], esp=esp)
        report("正在独立验证固件与恢复区域……", 0)
        esptool.main(["--chip", "esp32c3", "--port", port, "--no-stub", "--after", "no_reset_stub", "verify_flash",
                      hex(target.offset), str(image_path)], esp=esp)
        if esp.read_flash(target.offset, len(image_data)) != image_data:
            raise OSError("应用完整读回与发布镜像不一致，请重试刷机。")
        if esp.read_flash(0, 0x10000) != system or esp.flash_md5sum(recovery.offset, recovery.size) != recovery_md5:
            raise OSError("刷后系统区／恢复区检查失败，请保留日志和备份。")
        record["verified"] = True
        journal.write_text(json.dumps(record, indent=2), encoding="utf-8")
        report("固件校验通过，正在重启……", 1)
        return mac
    finally:
        if esp is not None:
            try:
                esp.hard_reset(uses_usb=True)
            finally:
                esp._port.close()
