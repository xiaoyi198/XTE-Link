"""Apply the existing SDCardManager patch to the pinned SDK; no device access."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "open-x4-sdk"
PATCH = ROOT / "patches/open-x4-sdk-sdcard-format.patch"
COMMIT = "7d86603ad27709a9a766bb5ad893cfc39e60777e"


def git(*args):
    return subprocess.run(["git", "-C", str(SDK), *args],
                          capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate without changing files")
    args = parser.parse_args()
    if not (SDK / ".git").exists():
        parser.error("SDK not initialized; run git submodule update --init --recursive")
    revision = git("rev-parse", "HEAD")
    if revision.returncode or revision.stdout.strip() != COMMIT:
        parser.error("SDK commit differs from the pinned version; no files changed")
    if git("apply", "--reverse", "--check", str(PATCH)).returncode == 0:
        print("SDK patch already applied")
        return
    check = git("apply", "--check", str(PATCH))
    if check.returncode:
        parser.error("SDK patch cannot apply; preserve local changes and resolve manually:\n" + check.stderr)
    if args.check:
        print("SDK patch can be applied")
        return
    applied = git("apply", str(PATCH))
    if applied.returncode:
        parser.error(applied.stderr)
    print("SDK patch applied")


if __name__ == "__main__":
    main()
