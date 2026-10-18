"""Exercise actual TF BMP/overlay/render code against independently decoded assets."""
import json
import argparse
import shutil
from pathlib import Path
import subprocess
from PIL import Image
from verify_tf_font import ROOT, TESTS, OUT, compile_host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--misans-dir', type=Path, required=True, help='locally generated fonts/ directory')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = ROOT / 'assets/ui/gray-flat-v1'
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    fixtures = OUT / 'ui-fixtures'
    fixtures.mkdir(exist_ok=True)
    setup = []
    for i, item in enumerate(manifest['assets']):
        with Image.open(bundle / item['png']) as image:
            (fixtures / f'{i}.gray').write_bytes(image.convert('L').tobytes())
        local = (bundle / item['bmp']).relative_to(ROOT).as_posix()
        expected = (fixtures / f'{i}.gray').relative_to(ROOT).as_posix()
        setup.append(f'  check(R"({local})", R"({item["tf_path"]})", {item["size"]}, R"({expected})");')
    for size in (24, 32, 40, 64):
        name = f'MiSans-Medium-{size}.epdfont'
        shutil.copyfile(args.misans_dir / name, fixtures / name)
        path = (fixtures / name).relative_to(ROOT).as_posix()
        setup.append(f'host::files["/fonts/{name}"] = read(R"({path})");')
    source = (ROOT / 'firmware/src/main.cpp').read_text(encoding='utf-8')
    state = source[source.index('constexpr uint32_t OFFLINE_MS'):source.index('constexpr uint32_t POWER_HOLD_MS')]
    functions = source[source.index('class FrameGraphics'):source.index('void sleepUntilPowerButton')]
    tests = r'''
#include <cassert>
std::vector<uint8_t> read(const char* name) {
  std::ifstream in(name, std::ios::binary); assert(in);
  return {std::istreambuf_iterator<char>(in), {}};
}
bool bit(int x, int y) { return (display.bytes[y * 100 + x / 8] & (0x80 >> (x & 7))) != 0; }
void check(const char* local, const char* remote, int size, const char* pixels) {
  host::files[remote] = read(local);
  const auto expected = read(pixels);
  for (bool inverse : {false, true}) for (auto plane : {xte::SleepImage::BASE, xte::SleepImage::LSB, xte::SleepImage::MSB}) {
    xte::SleepImage image;
    assert(image.openIcon(remote, static_cast<uint16_t>(size)));
    display.bytes.fill(plane == xte::SleepImage::BASE ? 255 : 0);
    assert(image.drawAt(display.bytes.data(), 13, 17, plane, inverse));
    for (int y = 0; y < 480; ++y) for (int x = 0; x < 800; ++x) {
      bool want = plane == xte::SleepImage::BASE;
      if (x >= 13 && x < 13+size && y >= 17 && y < 17+size) {
        int level = expected[(y-17)*size+x-13] / 85;
        if (inverse) level = 3-level;
        want = plane == xte::SleepImage::BASE ? level == 3 : plane == xte::SleepImage::LSB ? level == 1 : level == 1 || level == 2;
      }
      assert(bit(x,y) == want);
    }
    assert(!image.drawAt(display.bytes.data(), -1, 0));
    assert(!image.drawAt(display.bytes.data(), 800-size+1, 0));
    image.close(); assert(host::opens == 0);
  }
}
void saveGray(const std::string& path) {
  std::ofstream out(path, std::ios::binary);
  for (int i=0; i<384000; ++i) {
    const uint8_t mask = 0x80 >> (i & 7);
    const uint8_t value = display.grayLsb[i/8] & mask ? 85 : display.grayMsb[i/8] & mask ? 170 : display.base[i/8] & mask ? 255 : 0;
    out.put(static_cast<char>(value));
  }
}
int main(int argc, char** argv) {
  assert(argc == 3);
  host::bytes = read(argv[1]);
__SETUP__
  const std::string path = "/xte-link/ui/gray-flat-v1/computer-64.bmp";
  const auto good = host::files[path];
  for (int kind=0; kind<7; ++kind) {
    auto bad=good;
    if (kind==0) bad.resize(40);
    if (kind==1) bad.resize(69);
    if (kind==2) bad[18]=65;
    if (kind==3) bad[28]=24;
    if (kind==4) bad[30]=1;
    if (kind==5) for (int n=14;n<18;++n) bad[n]=255;
    if (kind==6) for (int n=10;n<14;++n) bad[n]=255;
    host::files[path]=bad;
    xte::SleepImage image; assert(!image.openIcon(path.c_str(),64)); assert(host::opens==0);
  }
  host::files[path]=good;
  // A top-down BMP must draw exactly like the original bottom-up BMP.
  auto flipped=good;
  const int32_t negative=-64; memcpy(flipped.data()+22,&negative,4);
  for (int y=0;y<64;++y) memcpy(flipped.data()+70+y*32,good.data()+70+(63-y)*32,32);
  display.bytes.fill(255);
  { xte::SleepImage image; assert(image.openIcon(path.c_str(),64)); assert(image.drawAt(display.bytes.data(),3,4)); }
  const auto bottom=display.bytes;
  host::files[path]=flipped; display.bytes.fill(255);
  { xte::SleepImage image; assert(image.openIcon(path.c_str(),64)); assert(image.drawAt(display.bytes.data(),3,4)); }
  assert(bottom==display.bytes); host::files[path]=good;
  // Mid-row SD failure clears the asset rectangle before the old GFX fallback.
  uiAssets.begin(display.bytes.data()); display.bytes.fill(255); host::failAfter=host::reads+7;
  assert(!uiAssets.draw(xte::UiAssets::COMPUTER,13,17));
  assert(std::all_of(display.bytes.begin(),display.bytes.end(),[](uint8_t b){return b==255;}));
  assert(host::opens==0); host::failAfter=-1;
  uiAssets.begin(display.bytes.data());
  for(int i=0;i<8;++i) assert(uiAssets.draw(xte::UiAssets::COMPUTER,13,17));
  assert(!uiAssets.draw(xte::UiAssets::COMPUTER,13,17));
  assert(uiAssets.drawPlane(xte::SleepImage::LSB) && host::opens==0);
  assert(!uiAssets.drawPlane(xte::SleepImage::BASE));
  // Real production pages: gray passes restore BW, preserve focus and close files.
  std::string out=argv[2]; usbPowered=true;
  render(false,true); assert(font.healthy() && uiAssets.loaded()==13 && !uiAssets.missing());
  assert(display.bytes==display.base && host::opens==0); saveGray(out+"/menu.gray");
  menuPage=1; render(false,false); assert(!uiAssets.missing()); saveGray(out+"/menu-page2.gray");
  appMenu=false; page=Page::SETTINGS;
  for (uint8_t i=0;i<6;++i) {
    selectedGroup=i; render(false,false); assert(!uiAssets.missing() && uiAssets.loaded()==15);
    assert(display.bytes==display.base && host::opens==0);
    saveGray(out+"/settings-"+std::to_string(i)+".gray");
  }
  haveStatus=true; status.volume=65;
  page=Page::VOLUME; render(true,false); assert(font.healthy() && display.bytes==display.base && host::opens==0);
  saveGray(out+"/volume.gray");
  page=Page::SETTINGS; settingsMode=SettingsMode::INFO; selectedGroup=5;
  render(true,false); assert(font.healthy() && display.bytes==display.base && host::opens==0);
  saveGray(out+"/settings-info.gray");
  page=Page::STATUS; render(true,false); assert(font.healthy() && !uiAssets.missing()); saveGray(out+"/status.gray");
  const auto saved=host::files; host::files.clear();
  appMenu=true; menuPage=0; const auto grayCount=display.grayDisplays;
  render(false,false); assert(uiAssets.loaded()==0 && uiAssets.missing()==13 && !uiAssets.hasGray());
  assert(display.grayDisplays==grayCount && host::opens==0);
  saveFrame(out+"/missing.raw");
  puts("PASS: all 30 BMPs match independent PNG pixels in three planes and inverse; malformed/top-down/bounds/SD failures, bounded overlays, real menus/settings, BW restoration and missing-card fallback.");
}
'''.replace('__SETUP__', '\n'.join(setup))
    cpp = OUT / 'ui-assets-test.cpp'
    cpp.write_text('#include "ui_host_prefix.h"\n#include "sleep_art.h"\n'+state+functions+tests, encoding='utf-8')
    exe=compile_host(cpp, 'ui-assets-test', ui=True)
    font = args.misans_dir / 'MiSans-Medium-32.epdfont'
    shutil.copyfile(font, fixtures/'font.epdfont')
    subprocess.run([str(exe),(fixtures/'font.epdfont').relative_to(ROOT).as_posix(),fixtures.relative_to(ROOT).as_posix()], cwd=ROOT, check=True)
    for file in fixtures.glob('*.gray'):
        if len(file.read_bytes()) == 384000:
            Image.frombytes('L',(800,480),file.read_bytes()).save(fixtures/(file.stem+'.png'))
    Image.frombytes('1',(800,480),(fixtures/'missing.raw').read_bytes()).save(fixtures/'missing.png')


if __name__=='__main__':
    main()
