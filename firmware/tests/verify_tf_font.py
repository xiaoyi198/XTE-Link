"""Build the production font reader on the host, compare pixels, and render firmware pages.
Run from the project root: .venv/Scripts/python.exe firmware/tests/verify_tf_font.py
Uses MSVC Build Tools on Windows, or g++/clang++ elsewhere; no device or serial port.
"""
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/font_preview'))
from preview_epdfont import EpdFont, gb2312_codepoints

TESTS = ROOT / 'firmware/tests'
OUT = ROOT / '.pio/font-host-test'
FONT = ROOT / 'backups/fonts/Zheng20号.epdfont'

def compile_host(source, name, ui=False):
    includes = [TESTS / 'stubs', ROOT / 'firmware/src', TESTS]
    if ui:
        includes += [ROOT / '.pio/libdeps/xteink_x4/ArduinoJson/src',
                     ROOT / '.pio/libdeps/xteink_x4/Adafruit GFX Library']
    exe = OUT / (name + ('.exe' if sys.platform == 'win32' else ''))
    if sys.platform == 'win32':
        vswhere = Path('C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe')
        installation = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
            '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '-property', 'installationPath'], text=True).strip()
        if not installation:
            raise RuntimeError('MSVC C++ Build Tools not found')
        args = ['cl', '/nologo', '/std:c++17', '/EHsc', '/utf-8', '/W4', '/WX', '/D_CRT_SECURE_NO_WARNINGS']
        args += ['/I' + str(p) for p in includes]
        if ui:
            args += ['/DHOST_UI', '/wd4244']  # GFX APIs intentionally use int16 coordinates.
        args += [str(source), '/Fo' + str(OUT / (name + '.obj')), '/Fe' + str(exe)]
        vcvars = Path(installation) / 'VC/Auxiliary/Build/vcvars64.bat'
        command = OUT / (name + '-build.cmd')
        command.write_text('@echo off\ncall "' + str(vcvars) + '" >nul\n' +
                           subprocess.list2cmdline(args) + '\n', encoding='utf-8')
        subprocess.run(['cmd', '/c', str(command)], cwd=ROOT, check=True)
    else:
        compiler = shutil.which('g++') or shutil.which('clang++')
        if not compiler:
            raise RuntimeError('g++ / clang++ not found')
        args = [compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror']
        args += ['-I' + str(p) for p in includes]
        if ui:
            args += ['-DHOST_UI']
        subprocess.run(args + [str(source), '-o', str(exe)], check=True)
    return exe

def png(path, width, height, bits):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    stride = (width + 7) // 8
    rows = b''.join(b'\0' + bits[y * stride:(y + 1) * stride] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 1, 0, 0, 0, 0)) +
        chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))

def verify_pixels(font, codepoints, actual):
    assert len(actual) == len(codepoints) * 518
    for i, cp in enumerate(codepoints):
        got_cp, got_width = struct.unpack_from('<IH', actual, i * 518)
        assert got_cp == cp
        glyph = font.glyph(cp)
        pixels = bytearray(512)
        if glyph:
            w, h, advance, left, top, _, offset = glyph
            for row in range(h):
                for col in range(w):
                    n = row * w + col
                    level = (font.data[font.bitmap_offset + offset + n // 4] >> (6 - 2 * (n % 4))) & 3
                    dx, y = left + col, 8 + font.ascender - top + row
                    x = 4 + dx
                    if level >= 2 and 0 <= dx < 56 and 0 <= x < 64 and 0 <= y < 64:
                        pixels[y * 8 + x // 8] |= 0x80 >> (x % 8)
        else:
            advance = 42
            for row in range(26):
                for col in range(26):
                    if row in (0, 25) or col in (0, 25):
                        x, y = 12 + col, 15 + row
                        pixels[y * 8 + x // 8] |= 0x80 >> (x % 8)
        assert got_width == advance, f'U+{cp:04X}: advance'
        assert actual[i * 518 + 6:(i + 1) * 518] == pixels, f'U+{cp:04X}: bitmap differs'
    print(f'PASS: {len(codepoints)} GB2312 / ASCII cases match offline-preview pixels exactly')

def render_pages(font_dir=None):
    # Compile the unchanged production render functions, rather than a second layout.
    main = (ROOT / 'firmware/src/main.cpp').read_text(encoding='utf-8')
    state_source = main[main.index('constexpr uint32_t OFFLINE_MS'):main.index('constexpr uint32_t POWER_HOLD_MS')]
    render_source = main[main.index('class FrameGraphics'):main.index('void sleepUntilPowerButton')]
    source = OUT / 'ui-test.cpp'
    source.write_text('#include "ui_host_prefix.h"\n#include "sleep_art.h"\n' + state_source + render_source + r'''
int main(int argc, char** argv) {
  if (argc != 3) return 1;
  std::ifstream fontInput(argv[1], std::ios::binary);
  host::bytes.assign(std::istreambuf_iterator<char>(fontInput), {});
  // LOAD_MISANS_FIXTURES
  std::string out = argv[2];
  render(false, true); saveFrame(out + "/menu.raw");
  for (int i = 1; i < 4; ++i) {
    menuSelection[0] = i;
    render(false, false); saveFrame(out + "/menu-" + std::to_string(i) + ".raw");
  }
  menuPage = 1;
  for (int i = 0; i < 4; ++i) {
    menuSelection[1] = i;
    render(false, false); saveFrame(out + "/menu-page2-" + std::to_string(i) + ".raw");
  }
  menuPage = 0;
  appMenu = false; page = Page::STATUS;
  render(false, true); saveFrame(out + "/waiting.raw");
  haveStatus = true;
  strcpy(status.host, "DESKTOP-X4"); strcpy(status.time, "2026-10-13 12:34:56");
  status.cpu = 24.5f; status.gpu = 36; status.gpuTemp = 58;
  status.memoryUsed = 8192; status.memoryTotal = 32768; status.volume = 65;
  render(true, true); saveFrame(out + "/status.raw");
  page = Page::VOLUME;
  render(true, false); saveFrame(out + "/volume.raw");
  page = Page::AGENTS; status.agentCount = 1;
  strcpy(status.agents[0].tool, "codex"); strcpy(status.agents[0].state, "ask");
  strcpy(status.agents[0].text, "Allow command execution?");
  render(true, false); saveFrame(out + "/agent-ask.raw");
  strcpy(status.agents[0].state, "running"); status.agents[0].text[0] = 0;
  render(true, false); saveFrame(out + "/agent-running.raw");
  page = Page::SETTINGS; settingsMode = SettingsMode::HOME;
  render(true, false); saveFrame(out + "/settings-preview.raw");
  saveFrame(out + "/settings-home.raw");
  for (uint8_t group = 0; group < GROUP_COUNT; ++group) {
    selectedGroup = group; selectedSetting = GROUP_ITEMS[group][0]; settingsMode = SettingsMode::LIST;
    render(true, false); saveFrame(out + "/settings-group-" + std::to_string(group) + ".raw");
  }
  selectedGroup = 0; selectedSetting = UPDATE;
  settingsMode = SettingsMode::LIST;
  render(true, false); saveFrame(out + "/settings-list.raw");
  selectedSetting = GHOST; settingsMode = SettingsMode::EDIT; draftSetting = 2;
  render(true, false); saveFrame(out + "/settings-edit.raw");
  settingsMessage = "保存失败";
  render(true, false); saveFrame(out + "/settings-save-error.raw");
  settingsMessage = nullptr; settingsMode = SettingsMode::INFO; selectedGroup = 5;
  render(true, false); saveFrame(out + "/settings-info.raw");
  settingsMode = SettingsMode::RESET;
  render(true, false); saveFrame(out + "/settings-reset.raw");
  settingsMessage = "串口切换失败，请重试";
  render(true, false); saveFrame(out + "/settings-reset-error.raw");
  settingsMessage = nullptr;
  selectedGroup = 3; settingsMode = SettingsMode::CONNECTION;
  render(true, false); saveFrame(out + "/settings-connection.raw");
  selectedGroup = 1; settingsMode = SettingsMode::GALLERY;
  render(true, false); saveFrame(out + "/settings-gallery-empty.raw");
  gallery = {String("/.sleep/风景.bmp"), String("/.sleep/桌面.bmp"), String("/sleep.bmp")};
  gallerySelection = 1; settings.imageMode = 1; strcpy(settings.sleepFile, "/.sleep/桌面.bmp");
  render(true, false); saveFrame(out + "/settings-gallery.raw");
  settingsMessage = "已设为固定屏保";
  render(true, false); saveFrame(out + "/settings-gallery-saved.raw");
  settingsMessage = nullptr; gallery.clear();
  selectedGroup = 5; selectedSetting = SLEEP_TIMEOUT; settingsMode = SettingsMode::EDIT; draftSetting = 1;
  render(true, false); saveFrame(out + "/settings-sleep-timeout.raw");
  page = Page::CLASH;
  clash.complete = true; clash.count = 5; clash.pending = false;
  const char* clashRows[] = {"订阅列表", "节点选择", "系统代理：开启", "TUN：关闭", "刷新状态"};
  for (uint8_t i = 0; i < 5; ++i) {
    strcpy(clash.rows[i].name, clashRows[i]);
    clash.rows[i].enabled = true;
  }
  strcpy(clash.message, "开关即时生效，Verge 重载可能恢复");
  render(true, false); saveFrame(out + "/clash-home.raw");
  clash.confirming = true; clash.selected = 2;
  render(true, false); saveFrame(out + "/clash-proxy-confirm.raw");
  clash.selected = 3;
  render(true, false); saveFrame(out + "/clash-tun-confirm.raw");
  clash.confirming = false;
  strcpy(clash.view, "nodes"); clash.count = 3; clash.selected = 1;
  strcpy(clash.rows[0].name, "日本 东京 01");
  strcpy(clash.rows[1].name, "香港 高速节点 02");
  strcpy(clash.rows[2].name, "新加坡 备用节点");
  clash.rows[0].current = true;
  strcpy(clash.message, "手选节点，仅本次运行");
  render(true, false); saveFrame(out + "/clash-nodes.raw");
  clash.confirming = true;
  render(true, false); saveFrame(out + "/clash-confirm.raw");
  clash.confirming = false; clash.complete = false;
  strcpy(clash.message, "驱动未响应，请更新电脑驱动");
  render(false, false); saveFrame(out + "/clash-error.raw");
  hostNow = 1000; lastContact = 1000; haveStatus = true;
  status.agentCount = 1; strcpy(status.agents[0].state, "ask"); strcpy(status.agents[0].requestId, "preview");
  appMenu = true; menuPage = 0;
  render(true, false); saveFrame(out + "/menu-approval-badge.raw");
  appMenu = false; page = Page::STATUS;
  render(true, false); saveFrame(out + "/status-approval-badge.raw");
  renderSleep(); saveFrame(out + "/sleep.raw");
  host::present = false;
  host::files.clear();
  appMenu = false; page = Page::SETTINGS;
  render(false, false); saveFrame(out + "/settings-missing-font.raw");
  appMenu = true;
  render(false, false); saveFrame(out + "/menu-missing-font.raw");
  menuPage = 1;
  render(false, false); saveFrame(out + "/menu-page2-missing-font.raw");
  host::present = false; appMenu = false; page = Page::STATUS;
  render(false, false); saveFrame(out + "/missing-font.raw");
  return host::opens ? 1 : 0;
}
''', encoding='utf-8')
    if font_dir is not None:
        setup = []
        for size in (24, 32, 40, 64):
            name = f'MiSans-Medium-{size}.epdfont'
            path = (font_dir / name).relative_to(ROOT).as_posix()
            setup.append(f'{{ std::ifstream in(R"({path})", std::ios::binary); '
                         f'host::files["/fonts/{name}"].assign(std::istreambuf_iterator<char>(in), {{}}); }}')
        source.write_text(source.read_text(encoding='utf-8').replace('// LOAD_MISANS_FIXTURES', '\n'.join(setup)), encoding='utf-8')
    exe = compile_host(source, 'ui-test', ui=True)
    subprocess.run([str(exe), str(FONT), str(OUT)], check=True)
    # Fail if a fixed UI label would silently clip or show missing characters.
    import re
    font = EpdFont(FONT)
    labels = re.findall(r'"([^"\n]*[\u4e00-\u9fff][^"\n]*)"', render_source)
    for label in labels:
        assert all(font.glyph(ord(ch)) is not None for ch in label), f'missing UI glyph: {label}'
    # Setting descriptions are one full-width line; never silently clip a hint.
    help_source = render_source[render_source.index('const char* settingHelp'):render_source.index('uint8_t settingIndex')]
    for label in re.findall(r'"([^"\n]+)"', help_source):
        assert sum(font.glyph(ord(ch))[2] for ch in label) <= 704, f'clipped setting help: {label}'
    for label, width in [('CPU负载', 312), ('GPU温度', 312), ('内存用量', 312),
                         ('等待连接', 202), ('确认：批准', 380), ('右键：拒绝', 260),
                         ('等待确认', 232), ('上下键切换助手', 752),
                         ('屏幕更新间隔', 440), ('恢复默认值', 440), ('保存失败', 272),
                         ('左右翻页  上下选择  确认打开', 744),
                         ('左右翻页  上下选择  功能占位', 744),
                         ('上下选择  左右翻页  确认进入', 744),
                         ('驱动未响应，请更新电脑驱动', 704),
                         ('确认执行此操作？', 704),
                         ('快捷功能', 220), ('占位二', 220), ('占位三', 220),
                         ('上下选择  确认打开  返回菜单', 744),
                         ('左右调整  确认保存  返回取消', 744),
                         ('音量步长 5%，更新间隔 5 秒', 704)]:
        assert sum(font.glyph(ord(ch))[2] for ch in label) <= width, f'clipped label: {label}'
    for path in OUT.glob('*.raw'):
        png(path.with_suffix('.png'), 800, 480, path.read_bytes())
    print('PASS: UI frames rendered from actual firmware functions; font closed before each display')

def main():
    import argparse
    global FONT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, default=FONT, help='path to the supplied Zheng20 font')
    parser.add_argument('--misans-dir', type=Path, help='optional generated fonts/ directory for current UI frames')
    args = parser.parse_args()
    FONT = args.font.resolve()
    if not FONT.is_file():
        parser.error('font file not found; provide --font PATH (fonts are not included in Git)')
    OUT.mkdir(parents=True, exist_ok=True)
    font = EpdFont(FONT)
    shutil.copyfile(FONT, OUT / 'font20.epdfont')
    codepoints = sorted(gb2312_codepoints() | set(range(32, 127)))
    (OUT / 'codepoints.txt').write_text(''.join(f'{cp:x}\n' for cp in codepoints), encoding='ascii')
    exe = compile_host(TESTS / 'tf_font_test.cpp', 'font-test')
    subprocess.run([str(exe), str(OUT / 'font20.epdfont'), str(OUT / 'codepoints.txt'),
                    str(OUT / 'pixels.bin')], check=True)
    verify_pixels(font, codepoints, (OUT / 'pixels.bin').read_bytes())
    if args.misans_dir:
        render_pages(args.misans_dir.resolve())
        print('Native-size firmware screenshots:', OUT)

if __name__ == '__main__':
    main()
