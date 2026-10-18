"""Exercise the actual pinned/patched C receiver with deterministic serial faults."""
import binascii
import ctypes as C
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.pio/ymodem-host-test'
SOURCE = ROOT / 'third_party/esp32-ymodem/components/ymodem/ymodem.c'
READ = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_uint, C.c_uint)
WRITE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_uint8), C.c_uint)
PENDING = C.CFUNCTYPE(C.c_int, C.c_void_p)


class IO(C.Structure):
    _fields_ = [('context', C.c_void_p), ('read', READ), ('write', WRITE),
                ('store', WRITE), ('control_pending', PENDING), ('expected_size', C.c_uint)]


def compile_receiver():
    OUT.mkdir(parents=True, exist_ok=True)
    library = OUT / ('ymodem.dll' if sys.platform == 'win32' else 'ymodem.so')
    if sys.platform == 'win32':
        vswhere = Path('C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe')
        installation = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
            '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
            '-property', 'installationPath'], text=True).strip()
        vcvars = Path(installation) / 'VC/Auxiliary/Build/vcvars64.bat'
        args = ['cl', '/nologo', '/TC', '/LD', '/utf-8', '/W3', '/DXTE_YMODEM_RECEIVE_ONLY',
                str(SOURCE), '/Fo' + str(OUT / 'ymodem.obj'), '/link', '/OUT:' + str(library), '/EXPORT:Ymodem_Receive']
        command = OUT / 'build.cmd'
        command.write_text('@echo off\ncall "' + str(vcvars) + '" >nul\n' + subprocess.list2cmdline(args) + '\n', encoding='utf-8')
        subprocess.run(['cmd', '/c', str(command)], cwd=ROOT, check=True)
    else:
        subprocess.run([shutil.which('cc'), '-shared', '-fPIC', '-DXTE_YMODEM_RECEIVE_ONLY',
                        str(SOURCE), '-o', str(library)], check=True)
    lib = C.CDLL(str(library))
    lib.Ymodem_Receive.argtypes = [C.POINTER(IO)]
    lib.Ymodem_Receive.restype = C.c_int
    return lib


def packet(sequence, data, size=1024):
    body = data.ljust(size, b'\x00')
    assert len(body) == size
    crc = binascii.crc_hqx(body, 0)
    return bytes([1 if size == 128 else 2, sequence & 255, 255 - (sequence & 255)]) + body + crc.to_bytes(2, 'big')


def frames(data, size=1024):
    header = packet(0, b'test.bin\0' + str(len(data)).encode(), size)
    blocks = [packet(i // size + 1, data[i:i + size], size) for i in range(0, len(data), size)]
    return [header, *blocks, b'\x04', b'\x04', packet(0, b'', size)]


def receive(lib, stream, expected, fail_store=False, fail_tx=False):
    incoming = bytearray(b''.join(stream))
    stored, replies = bytearray(), bytearray()

    @READ
    def read(context, destination, count, timeout):
        data = bytes(incoming[:count]); del incoming[:count]
        if data: C.memmove(destination, data, len(data))
        return len(data)

    @WRITE
    def write(context, data, count):
        replies.extend(C.string_at(data, count))
        return 0 if fail_tx else count

    @WRITE
    def store(context, data, count):
        if fail_store: return max(0, count - 1)
        stored.extend(C.string_at(data, count))
        return count

    @PENDING
    def pending(context): return int(not incoming)

    io = IO(None, read, write, store, pending, expected)
    result = lib.Ymodem_Receive(C.byref(io))
    return result, bytes(stored), bytes(replies)


def main():
    lib = compile_receiver()
    cases = 0
    for size in (0, 1, 127, 128, 129, 1023, 1024, 1025, 65539, 300 * 1024 + 17):
        data = (bytes(range(256)) * ((size + 255) // 256))[:size]
        result, stored, replies = receive(lib, frames(data), size)
        assert result == size and stored == data and replies.startswith(b'C') and replies.endswith(b'\x06'), (size, result)
        cases += 1
    data = bytes(range(256)) * 1030 + b'last'
    fs = frames(data)
    corrupt = bytearray(fs[2]); corrupt[5] ^= 1
    # CRC failure, lost header/data ACK, duplicate sequence 255 and 0, lost trailer ACK.
    injected = [fs[0], fs[0], fs[1], bytes(corrupt), fs[2], fs[2], *fs[3:255],
                fs[255], fs[255], fs[256], fs[256], *fs[257:], fs[-1]]
    result, stored, replies = receive(lib, injected, len(data))
    assert result == len(data) and stored == data and b'\x15' in replies, result
    cases += 1
    for fs, size, expected in (
        ([b'\x18\x18'], 10, -1),
        ([packet(0, b'x\0' + b'9' * 30)], 1, -4),
        ([packet(0, b'x' * 1024)], 1, -4),
        ([packet(0, b'x\0' + b'2')], 1, -9),
        ([packet(0, b'x\0' + b'0')], 1, -4),
        (frames(b'x')[:-3] + [b'\x04'], 2, -4),
        ([packet(0, b'x\0' + b'2'), packet(1, b'x', 128)[:50]], 2, -8),
        (frames(b'x')[:-1] + [packet(0, b'another-file')], 1, -12),
        ([packet(0, b'x\0' + b'1025'), packet(1, b'x' * 1024), b'\x04'], 1025, -11),
    ):
        result, stored, replies = receive(lib, fs, size)
        assert result == expected, (result, expected)
        cases += 1
    assert receive(lib, frames(b'abc'), 3, fail_store=True)[0] == -6
    assert receive(lib, frames(b'abc'), 3, fail_tx=True)[0] == -10
    cases += 2
    print(f'PASS: {cases} actual C receiver cases; binary boundaries, wrap, CRC/retries, cancellation, metadata, timeout, short storage write/full sink, USB failure')


if __name__ == '__main__': main()
