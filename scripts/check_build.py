"""PlatformIO pre-hook: fail before compiling an unpatched dependency."""
from pathlib import Path
import runpy

Import("env")

root = Path(env.subst("$PROJECT_DIR"))
runpy.run_path(str(root / "scripts/check_dependencies.py"))["check_dependencies"](root)
