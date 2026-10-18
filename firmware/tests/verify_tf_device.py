"""Opt-in COM-port tests. Only create files below a dedicated /xte-link/usb-test-* directory.

Close the tray driver first. No formatting, firmware writing, or user-file replacement.
The JSON report lists all created paths for review/cleanup with the existing TF tool.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import serial
from verify_ymodem import packet

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pc_driver.tf import TfClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--faults-only', action='store_true', help='Skip completed size/speed/download trials')
    args = parser.parse_args()
    assert args.directory.startswith('/xte-link/usb-test-') and len(args.directory) < 60
    connection = serial.serial_for_url(args.port, baudrate=115200, timeout=0.02, write_timeout=5, do_not_open=True)
    connection.dtr = False; connection.rts = False; connection.open()
    report = dict(device=args.device, directory=args.directory, rows=[], created=[])

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')

    def record(name, **fields):
        row = dict(test=name, **fields); report['rows'].append(row); save(); print(row, flush=True)

    class FaultSerial:
        fault = None
        phase = 'header'
        used = False
        @property
        def in_waiting(self): return connection.in_waiting
        def read(self, count):
            data = connection.read(count)
            if self.fault == 'drop-' + self.phase + '-ack' and not self.used and b'\x06' in data:
                data = data.replace(b'\x06', b'', 1); self.used = True
            return data
        def write(self, data):
            if len(data) in (133, 1029) and data[0] in (1, 2):
                self.phase = 'trailer' if data[1] == 0 and not any(data[3:-2]) else ('header' if data[1] == 0 and data[3:8] == b'test-' else f'data{data[1]}')
                if self.fault == 'crc' and self.phase == 'data2' and not self.used:
                    changed = bytearray(data); changed[-1] ^= 1; data = bytes(changed); self.used = True
            return connection.write(data)

    wire = FaultSerial()
    client = TfClient(wire)
    try:
        connection.write(b'{"v":2,"cmd":"hello"}\n')
        deadline = time.monotonic() + 3; info = bytearray()
        while time.monotonic() < deadline:
            info.extend(connection.read(connection.in_waiting or 1))
            if b'\n' in info: break
        assert ('"device":"' + args.device + '"').encode() in info, bytes(info)
        report['hello'] = info.decode(errors='replace')
        client.request('abort'); client.mount(); assert client.ymodem
        client.request('mkdir', path=args.directory)
        assert not list(client.list(args.directory)), 'Use a new empty test directory'
        report['root_before'] = list(client.list('/'))
        report['fonts_before'] = list(client.list('/fonts'))
        report['skins_before'] = list(client.list('/.sleep'))
        report['ui_before'] = list(client.list('/xte-link/ui/gray-flat-v1'))
        save()
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            def upload(name, size, legacy=False, fault=None):
                path = local / ('test-' + name + '.bin')
                data = (bytes(range(256)) * ((size + 255) // 256))[:size]
                path.write_bytes(data)
                remote = args.directory + '/' + name + '.bin'
                wire.fault = fault; wire.used = False; wire.phase = 'header'
                client.prefer_ymodem = not legacy
                report['created'].append(remote); save()
                started = time.monotonic()
                result = client.upload(path, remote)
                assert result == (size, hashlib.sha256(data).hexdigest()), result
                if fault: assert wire.used, fault
                wire.fault = None
                record(name, bytes=size, seconds=time.monotonic()-started, sha256=result[1], transport='legacy' if legacy else 'ymodem1k', fault=fault)
                return path, remote

            if args.faults_only:
                upload('boundary-1', 1)
            else:
                for size in (0, 1, 127, 128, 129, 1023, 1024, 1025): upload('boundary-' + str(size), size)
                for size, trials in ((65539, 3), (1048589, 3), (4194333, 1)):
                    for trial in range(trials): upload(f'fast-{size}-{trial}', size)
                for size in (65539, 1048589):
                    path, remote = upload('legacy-' + str(size), size, legacy=True)
                started = time.monotonic()
                downloaded = local / 'downloaded.bin'
                client.download(args.directory + '/fast-1048589-0.bin', downloaded)
                assert downloaded.read_bytes() == path.read_bytes()
                record('independent-legacy-download', bytes=1048589, seconds=time.monotonic()-started, byte_equal=True)
            for fault, size in (('crc', 65539), ('drop-header-ack', 65539), ('drop-data255-ack', 307217), ('drop-data0-ack', 307217), ('drop-trailer-ack', 65539)):
                upload('fault-' + fault, size, fault=fault)

            def expect_error(op, error, **fields):
                try: client.request(op, **fields)
                except OSError as exc: assert error in str(exc), str(exc)
                else: raise AssertionError(f'{op} unexpectedly succeeded: {error}')

            def absent(remote):
                names = {row['name'] for row in client.list(args.directory)}
                assert remote.rsplit('/',1)[-1] not in names and remote.rsplit('/',1)[-1] + '.xte-part' not in names

            # Existing destination must survive a refused put.
            expect_error('put', 'exists', path=args.directory+'/boundary-1.bin', size=1, sha256='0'*64, transport='ymodem1k')
            copy = local/'original-one.bin'; client.download(args.directory+'/boundary-1.bin', copy)
            assert copy.read_bytes() == b'\x00'
            record('no-overwrite', preserved=True)
            for name, expected_size, metadata_size, timeout in (('cancel', 10, 10, False), ('metadata', 1, 2, False), ('timeout', 10, 10, True)):
                remote = args.directory+'/'+name+'.bin'
                client.request('put', path=remote, size=expected_size, sha256='0'*64, transport='ymodem1k')
                assert client._read(1,2) == b'C'
                header = packet(0, b'test.bin\0'+str(metadata_size).encode())
                if timeout:
                    connection.write(header[:20])
                    deadline = time.monotonic()+15; received=bytearray()
                    while time.monotonic()<deadline and b'\x18\x18' not in received:
                        received.extend(client._read(1,0.2))
                    assert b'\x18\x18' in received, bytes(received)
                else:
                    connection.write(header)
                    if name == 'cancel':
                        assert client._read(2,2) == b'\x06C'
                        connection.write(b'\x18\x18'); assert client._read(1,2) == b'\x06'
                    else: assert client._read(2,2) == b'\x18\x18'
                time.sleep(0.2)
                expect_error('finish', 'ymodem_failed')
                client.request('abort'); absent(remote)
                record(name, recovered=True, partial_file_removed=True)
            remote = args.directory+'/bad-sha.bin'
            path = local/'test-bad-sha.bin'; path.write_bytes(b'incorrect declared hash')
            client.request('put', path=remote, size=path.stat().st_size, sha256='0'*64, transport='ymodem1k')
            client._send_ymodem(path); expect_error('finish','hash_mismatch'); absent(remote)
            record('sha-mismatch', recovered=True, partial_file_removed=True)
            upload('after-faults',1025)
        report['files_after'] = list(client.list(args.directory))
        for label, path in (('fonts','/fonts'),('skins','/.sleep'),('ui','/xte-link/ui/gray-flat-v1')):
            after = list(client.list(path))
            normalize = lambda rows: [{key:value for key,value in row.items() if key not in ('id','ok')} for row in rows]
            assert normalize(after) == normalize(report[label+'_before']), label
        record('resource-listings-preserved', fonts=True, skins=True, ui=True)
        report['passed'] = True; save()
    finally:
        connection.close()


if __name__ == '__main__': main()
