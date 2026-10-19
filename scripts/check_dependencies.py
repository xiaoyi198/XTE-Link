"""Read-only validation of pinned, already-patched firmware dependencies."""
import hashlib
from pathlib import Path
import runpy
import subprocess


def check_dependencies(root):
    root = Path(root).resolve()
    result = {}
    for name, script, directory_key, sources in (
        ("sdk", "apply_sdk_patch.py", "SDK", (
            "libs/hardware/SDCardManager/include/SDCardManager.h",
            "libs/hardware/SDCardManager/src/SDCardManager.cpp")),
        ("ymodem", "apply_ymodem_patch.py", "DEPENDENCY", (
            "components/ymodem/ymodem.c", "components/ymodem/ymodem.h",
            "components/ymodem/library.json")),
    ):
        # The existing appliers remain the single source of commit/patch pins.
        setup = runpy.run_path(str(root / "scripts" / script))
        directory, patch = setup[directory_key], setup["PATCH"]
        if not (directory / ".git").exists():
            raise RuntimeError(f"{name}: initialize dependencies with git submodule update --init --recursive")
        revision = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
        if revision.returncode or revision.stdout.strip() != setup["COMMIT"]:
            raise RuntimeError(f"{name}: commit differs from pinned {setup['COMMIT']}; preserve local changes and inspect the submodule")
        applied = subprocess.run(["git", "-C", str(directory), "apply", "--reverse", "--check", str(patch)],
                                 capture_output=True)
        if applied.returncode:
            raise RuntimeError(f"{name}: required patch is missing or conflicts; run python scripts/{script}; no files changed by this check")
        result[name] = {
            "commit": revision.stdout.strip(), "patch_applied": True,
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "sources": {(directory / path).relative_to(root).as_posix():
                        hashlib.sha256((directory / path).read_bytes()).hexdigest() for path in sources},
        }
    return result


if __name__ == "__main__":
    try:
        check_dependencies(Path(__file__).resolve().parents[1])
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    print("Pinned SDK/YMODEM commits and applied patches verified")
