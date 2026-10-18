"""Apply the pinned ESP32 YMODEM USB/SDK sink adaptation, idempotently."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY = ROOT / 'third_party/esp32-ymodem'
COMMIT = 'efb1466d00bc8f4f591c770cd349819675e83b83'
PATCH = ROOT / 'patches/ymodem-stream.patch'


def main():
    actual = subprocess.check_output(['git', '-C', str(DEPENDENCY), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != COMMIT:
        raise SystemExit(f'Unexpected YMODEM commit: {actual}')
    prefix = ['git', '-C', str(DEPENDENCY), 'apply']
    if subprocess.run(prefix + ['--reverse', '--check', str(PATCH)], capture_output=True).returncode == 0:
        print('YMODEM patch already applied')
        return
    subprocess.run(prefix + ['--check', str(PATCH)], check=True)
    subprocess.run(prefix + [str(PATCH)], check=True)
    print('YMODEM patch applied')


if __name__ == '__main__':
    main()
