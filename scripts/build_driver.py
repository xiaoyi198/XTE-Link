"""Build the Windows tray EXE and record its checksum and build dependencies."""

from datetime import datetime, timedelta, timezone
from importlib.metadata import distribution
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys


def check_requirements(path):
    """Check declared pins only; unrelated installed packages are allowed."""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            check_requirements(Path(path).parent / line[3:].strip())
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise RuntimeError(f"Unsupported build dependency pin: {line}")
        name, expected = match.groups()
        try:
            actual = distribution(name).version
        except ModuleNotFoundError as error:
            raise RuntimeError(f"Missing build dependency: {name}=={expected}; install pc_driver/requirements-build.txt") from error
        if actual != expected:
            raise RuntimeError(f"Build dependency {name}: expected {expected}, installed {actual}; install pc_driver/requirements-build.txt")


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, default=root / "artifacts/firmware/maintenance-20261019")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output", type=Path, help="new release directory; must not already exist")
    args = parser.parse_args()
    try:
        check_requirements(root / "pc_driver/requirements-build.txt")
    except RuntimeError as error:
        parser.error(str(error))
    from pc_driver.tray import icon_image
    from scripts.prepare_driver_payload import prepare
    payload = prepare(root, args.firmware)
    if args.prepare_only:
        print(f"Prepared: {payload}")
        return

    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    output = args.output.resolve() if args.output else root / "artifacts" / "pc-driver" / stamp
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
        "--collect-all", "esptool", "--hidden-import", "tools.misans.build_misans",
        "--add-data", str(payload) + ";install_payload",
        "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work), str(root / "pc_driver" / "tray.py"),
    ], cwd=root, check=True)
    exe = output / "XTE-Link.exe"
    checksum = hashlib.sha256(exe.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{checksum}  {exe.name}\n", encoding="ascii")
    frozen = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (output / "build-requirements.txt").write_text(frozen, encoding="utf-8")
    (output / "LICENSE-codex-mobile.txt").write_bytes((root / "pc_driver" / "LICENSE-codex-mobile").read_bytes())
    ymodem = distribution("ymodem")
    ymodem_license = next(path for path in ymodem.files if str(path).endswith("licenses/LICENSE"))
    (output / "LICENSE-ymodem.txt").write_bytes(ymodem.locate_file(ymodem_license).read_bytes())
    # The authoritative installation guide and licenses also live inside the EXE.
    import shutil
    for name in ("使用说明.txt", "THIRD-PARTY-NOTICES.txt", "corresponding-source.zip", "manifest.json"):
        shutil.copyfile(payload / name, output / name)
    print(f"Built: {exe}\nSHA-256: {checksum}")


if __name__ == "__main__":
    main()
