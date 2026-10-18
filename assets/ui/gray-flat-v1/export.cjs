// Offline export after the pinned upstream SVG files have been downloaded.
// Build-time dependency: sharp (version recorded in manifest). No firmware dependency.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const sharp = require('sharp');
const ROOT = __dirname;
const COMMIT = '2b75f3ad12b420c9504ef05df8d2564a28f8500e';
const BASE = `https://raw.githubusercontent.com/phosphor-icons/core/${COMMIT}`;
const specs = [
  ['computer', '电脑状态', 'desktop', 'menu', 64],
  ['volume', '系统音量', 'speaker-high', 'menu', 64],
  ['assistant', '助手控制', 'robot', 'menu', 64],
  ['settings', '设置', 'sliders-horizontal', 'menu', 64],
  ['shortcuts', '快捷功能', 'lightning', 'menu', 64],
  ['clash', 'Clash Verge', 'tree-structure', 'menu', 64],
  ['note', '占位二', 'note', 'menu', 64],
  ['add', '占位三', 'plus-square', 'menu', 64],
  ['display', '显示与刷新', 'monitor', 'settings', 32],
  ['gallery', '屏保图库', 'images', 'settings', 32],
  ['startup', '启动与操作', 'play-circle', 'settings', 32],
  ['connection', '连接与配对', 'plugs-connected', 'settings', 32],
  ['reminder', '助手提醒', 'bell', 'settings', 32],
  ['power', '设备与电源', 'power', 'settings', 32],
  ['usb', 'USB', 'usb', 'status', 24],
  ['wifi', 'Wi-Fi', 'wifi-high', 'status', 24],
  ['offline', '离线', 'wifi-slash', 'status', 24],
  ['tf-card', 'TF 卡', 'sim-card', 'status', 24],
  ['battery', '电池', 'battery-high', 'status', 24],
  ['external-power', '外部供电', 'plug', 'status', 24],
  ['up', '↑', 'arrow-up', 'keys', 16],
  ['down', '↓', 'arrow-down', 'keys', 16],
  ['left', '←', 'arrow-left', 'keys', 16],
  ['right', '→', 'arrow-right', 'keys', 16],
  ['confirm', '✓', 'check', 'keys', 16],
  ['back', '×', 'x', 'keys', 16],
].map(([id, label, upstream, role, size]) => ({id, label, upstream, role, size,
  weight: ['menu', 'settings'].includes(role) ? 'duotone' : 'bold'}));
const sha = data => crypto.createHash('sha256').update(data).digest('hex');
async function write(file, data) {
  const dest = path.join(ROOT, file);
  await fs.mkdir(path.dirname(dest), {recursive: true});
  await fs.writeFile(dest, data);
}
async function upstream(file) {
  const dest = `sources/phosphor/${file}`;
  try { return await fs.readFile(path.join(ROOT, dest), 'utf8'); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  if (!process.argv.includes('--fetch')) throw new Error(`Missing ${dest}; run with --fetch once.`);
  const url = file === 'LICENSE' ? `${BASE}/LICENSE` : `${BASE}/assets/${file.split('/')[0]}/${file.split('/')[1]}`;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status}: ${url}`);
  const data = await response.text();
  await write(dest, data);
  return data;
}
// BI_RGB bottom-up palette BMP; equivalent to the project's sleep BMP palette,
// with arbitrary square dimensions and DWORD-padded rows for UI assets.
function bmp(pixels, width, height, mono) {
  const bpp = mono ? 1 : 4;
  const colors = mono ? [0, 255] : [0, 85, 170, 255];
  const stride = Math.ceil(width * bpp / 32) * 4;
  const offset = 54 + colors.length * 4;
  const data = Buffer.alloc(offset + stride * height);
  data.write('BM'); data.writeUInt32LE(data.length, 2); data.writeUInt32LE(offset, 10);
  data.writeUInt32LE(40, 14); data.writeInt32LE(width, 18); data.writeInt32LE(height, 22);
  data.writeUInt16LE(1, 26); data.writeUInt16LE(bpp, 28); data.writeUInt32LE(stride * height, 34);
  data.writeInt32LE(2835, 38); data.writeInt32LE(2835, 42);
  data.writeUInt32LE(colors.length, 46); data.writeUInt32LE(colors.length, 50);
  colors.forEach((v, i) => data.fill(v, 54 + i * 4, 57 + i * 4));
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const v = pixels[y * width + x];
    const base = offset + (height - 1 - y) * stride;
    if (mono) data[base + (x >> 3)] |= (v === 255 ? 1 : 0) << (7 - (x & 7));
    else data[base + (x >> 1)] |= Math.round(v / 85) << (x % 2 ? 0 : 4);
  }
  return data;
}
async function raster(input, size, mono = false) {
  const data = await sharp(input).resize(size, size).flatten({background: '#fff'})
    .greyscale().raw().toBuffer();
  for (let i = 0; i < data.length; i++) data[i] = mono ? (data[i] < 128 ? 0 : 255) : Math.round(data[i] / 85) * 85;
  return data;
}
async function png(pixels, size) {
  return sharp(pixels, {raw: {width: size, height: size, channels: 1}}).png().toBuffer();
}
const outputs = [];
async function exportRaster(id, size, pixels, mono) {
  const pngPath = `png/${id}-${size}.png`, bmpPath = `tf/xte-link/ui/gray-flat-v1/${id}-${size}.bmp`;
  const p = await png(pixels, size), b = bmp(pixels, size, size, mono);
  await write(pngPath, p); await write(bmpPath, b);
  outputs.push({id, size, bpp: mono ? 1 : 4, png: pngPath, bmp: bmpPath,
    tf_path: '/' + bmpPath.slice(3), bytes: b.length, sha256: sha(b), png_sha256: sha(p)});
}
function xml(v) { return v.replaceAll('&', '&amp;').replaceAll('<', '&lt;'); }
async function dataUri(file) { return `data:image/png;base64,${(await fs.readFile(path.join(ROOT, file))).toString('base64')}`; }
async function proof() {
  const parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1120"><rect width="1200" height="1120" fill="white"/><style>text{font-family:Microsoft YaHei,sans-serif;fill:#000}.sub{fill:#555}</style>'];
  const text = (x, y, s, value, cls='') => parts.push(`<text x="${x}" y="${y}" font-size="${s}" class="${cls}">${xml(value)}</text>`);
  const image = async (file, x, y, size) => parts.push(`<image href="${await dataUri(file)}" x="${x}" y="${y}" width="${size}" height="${size}"/>`);
  await image('png/logo-128.png', 44, 30, 128);
  text(194, 83, 38, 'XTE-Link'); text(194, 121, 19, '白底 · 灰度扁平 · 统一几何造型', 'sub');
  text(890, 52, 15, 'Logo 实际像素尺寸', 'sub');
  for (const [size, x] of [[64,890],[32,990],[24,1070]]) {
    await image(`png/logo-${size}.png`, x, 75, size); text(x, 160, 13, `${size} px`, 'sub');
  }
  const section = (y, title, subtitle) => {
    parts.push(`<path d="M44 ${y}H1156" stroke="#aaa"/>`);
    text(44, y+37, 21, title); text(300, y+37, 15, subtitle, 'sub');
  };
  section(191, '应用图标', '64 × 64 px / 四级灰度 / 按当前菜单顺序');
  const menu = specs.filter(s => s.role === 'menu');
  for (let i=0; i<menu.length; i++) {
    const s=menu[i], x=44+(i%4)*286, y=258+Math.floor(i/4)*142;
    await image(`png/${s.id}-64.png`, x+16, y, 64);
    text(x+100,y+28,20,s.label); text(x+100,y+57,13,s.id,'sub');
  }
  text(44, 551, 14, '快捷功能、占位二、占位三沿用现有占位含义。Clash 使用通用节点造型。', 'sub');
  section(580, '设置分类', '32 × 32 px / 四级灰度');
  const groups = specs.filter(s => s.role === 'settings');
  for (let i=0; i<groups.length; i++) {
    const s=groups[i], x=44+i*188;
    await image(`png/${s.id}-32.png`,x+12,643,32); text(x,710,16,s.label);
  }
  section(744, '状态图标', '24 × 24 px / 黑白 / 外部供电不表示正在充电');
  const statuses=specs.filter(s=>s.role==='status');
  for(let i=0;i<statuses.length;i++) {
    const s=statuses[i],x=44+i*188;
    await image(`png/${s.id}-24.png`,x+16,808,24);text(x,865,16,s.label);
  }
  section(902,'按键提示','16 × 16 px / 黑白 / 采用横屏视角方向');
  const keys=specs.filter(s=>s.role==='keys');
  const labels=['上','下','左','右','确认','返回'];
  for(let i=0;i<keys.length;i++) {
    const s=keys[i],x=44+i*188;
    await image(`png/${s.id}-16.png`,x+20,970,16);text(x,1020,16,labels[i]);
  }
  text(44,1090,14,'Phosphor 2.1.1 (MIT) · Logo: imagegen · 已接入 gray-flat-ui-20261018', 'sub');
  parts.push('</svg>');
  await write('preview.png',await sharp(Buffer.from(parts.join(''))).png().toBuffer());
}
async function main() {
  await upstream('LICENSE');
  await Promise.all(specs.map(async s => {
    const source=await upstream(`${s.weight}/${s.upstream}-${s.weight}.svg`);
    const adapted=source.replace('fill="currentColor"','fill="#000000"')
      .replaceAll('opacity="0.2"','opacity="0.3333333333"');
    await write(`svg/${s.id}.svg`,adapted);
    const mono=['status','keys'].includes(s.role);
    await exportRaster(s.id,s.size,await raster(Buffer.from(adapted),s.size,mono),mono);
  }));
  // Normalize generated whitespace for comparable optical size beside UI icons.
  const mark=await sharp(path.join(ROOT,'originals/logo-imagegen.png')).trim({threshold:30}).toBuffer();
  const logo=await sharp(mark).resize(512,512,{fit:'contain',background:'#fff'})
    .extend({top:40,bottom:40,left:40,right:40,background:'#fff'}).png().toBuffer();
  for(const size of [128,64,32,24]) await exportRaster('logo',size,await raster(logo,size),false);
  outputs.sort((a,b)=>a.id.localeCompare(b.id)||b.size-a.size);
  await write('manifest.json',JSON.stringify({name:'XTE-Link gray-flat-v1',created:'2026-10-18',
    palette:[0,85,170,255],background:255,dither:false,firmware_integrated:true,firmware_min_version:'gray-flat-ui-20261018',
    source:{repository:'https://github.com/phosphor-icons/core',version:'2.1.1',commit:COMMIT,license:'MIT'},
    exporter:{sharp:sharp.versions.sharp},icons:specs,assets:outputs},null,2)+'\n');
  await proof();
  const hashes=[];
  async function walk(dir='') {
    for(const entry of (await fs.readdir(path.join(ROOT,dir),{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name))) {
      const file=path.posix.join(dir,entry.name);
      if(entry.isDirectory()) await walk(file);
      else if(file!=='SHA256SUMS.txt') hashes.push(`${sha(await fs.readFile(path.join(ROOT,file)))}  ${file}`);
    }
  }
  await walk(); await write('SHA256SUMS.txt',hashes.join('\n')+'\n');
  console.log(`Exported ${specs.length} icons, 4 logo sizes, ${outputs.length} BMPs; source ${COMMIT}.`);
}
main().catch(e=>{console.error(e);process.exitCode=1;});
