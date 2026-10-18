"""Host checks of the production settings/button functions; no device/NVS access."""
import subprocess
from verify_tf_font import ROOT, TESTS, OUT, compile_host


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    main_source = (ROOT / 'firmware/src/main.cpp').read_text(encoding='utf-8')
    state = main_source[main_source.index('constexpr uint32_t OFFLINE_MS'):
                        main_source.index('constexpr uint32_t POWER_HOLD_MS')]
    functions = main_source[main_source.index('class FrameGraphics'):
                            main_source.index('void sleepUntilPowerButton')]
    source = OUT / 'settings-test.cpp'
    source.write_text('#include "ui_host_prefix.h"\n#include "sleep_art.h"\n' + state + functions +
                      (TESTS / 'settings_test.cpp').read_text(encoding='utf-8'), encoding='utf-8')
    exe = compile_host(source, 'settings-test', ui=True)
    subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    main()
