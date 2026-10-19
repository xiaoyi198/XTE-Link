"""PlatformIO/SCons hook: archive a successful application build locally."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess

Import("env")


def archive_firmware(source, target, env):
    root = Path(env.subst("$PROJECT_DIR"))
    image = Path(env.subst("$BUILD_DIR/${PROGNAME}.bin"))
    data = image.read_bytes()
    if not data:
        raise RuntimeError("Refusing to archive an empty firmware image")
    digest = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone(timedelta(hours=8)))
    directory = root / "backups/firmware" / (now.strftime("%Y-%m-%d_%H%M%S_%f") + "-" + digest[:12])
    directory.mkdir(parents=True, exist_ok=False)
    destination = directory / "firmware.bin"
    shutil.copyfile(image, destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Archived firmware SHA-256 mismatch")
    sources = sorted((root / "firmware/src").glob("*.cpp")) + sorted((root / "firmware/src").glob("*.h"))
    sources += [root / "platformio.ini", root / "scripts/archive_firmware.py"]
    sources += [root / "scripts/check_build.py", root / "scripts/check_dependencies.py"]
    sources += [root / "scripts/apply_ymodem_patch.py", root / "patches/ymodem-stream.patch"]
    sources += [root / "scripts/apply_sdk_patch.py", root / "patches/open-x4-sdk-sdcard-format.patch",
                root / "patches/open-x4-sdk-sdcard-space.patch"]
    build_flags = [str(flag) for flag in env.get("BUILD_FLAGS", [])]
    if any("XTE_TF_BENCH" in flag for flag in build_flags):
        sources.append(root / "firmware/tests/tf_benchmark.h")
    sdk_commit = subprocess.check_output(["git", "-C", str(root / "open-x4-sdk"), "rev-parse", "HEAD"], text=True).strip()
    sdk_changes = subprocess.check_output(["git", "-C", str(root / "open-x4-sdk"), "status", "--porcelain"], text=True).strip()
    dependencies = runpy.run_path(str(root / "scripts/check_dependencies.py"))["check_dependencies"](root)
    manifest = {
        "created_shanghai": now.isoformat(timespec="microseconds"),
        "environment": env.subst("$PIOENV"),
        "build_flags": build_flags,
        "image": destination.name,
        "bytes": len(data),
        "sha256": digest,
        "git_head": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        "git_dirty": bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip()),
        "dependencies": dependencies,
        "sdk_commit": sdk_commit,
        "sdk_local_changes": sdk_changes,
        "ymodem_commit": subprocess.check_output(["git", "-C", str(root / "third_party/esp32-ymodem"), "rev-parse", "HEAD"], text=True).strip(),
        "ymodem_sources": {str(path.relative_to(root).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((root / "third_party/esp32-ymodem/components/ymodem").iterdir())
                           if path.name in ("ymodem.c", "ymodem.h", "library.json")},
        "source_hashes": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "scope": "application image; existing full Flash backups are retained separately",
    }
    # Write the manifest last so an incomplete archive never looks committed.
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Firmware archived: " + directory.relative_to(root).as_posix())


# The default build waits for both the binary and size validation. AlwaysBuild
# also archives a successful incremental build whose binary is already current.
archive = env.Alias("archive_firmware", ["buildprog", "checkprogsize"], archive_firmware)
env.AlwaysBuild(archive)
env.Default(archive)
