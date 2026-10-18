"""Run the real PC sender against the actual patched C receiver over a duplex link."""
import ctypes as C
from pathlib import Path
import sys
import tempfile
import threading
import time

from verify_ymodem import IO, READ, WRITE, PENDING, compile_receiver

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pc_driver.tf import TfClient


def transfer(lib, size, fault=None):
    condition = threading.Condition()
    incoming, outgoing, stored = bytearray(), bytearray(), bytearray()
    state = dict(phase='header', used=False, result=None)
    data = (bytes(range(256)) * ((size + 255) // 256))[:size]

    def take(queue, count, timeout):
        deadline = time.monotonic() + timeout
        with condition:
            while len(queue) < count and time.monotonic() < deadline:
                condition.wait(max(0, deadline - time.monotonic()))
            result = bytes(queue[:count]); del queue[:count]
            return result

    @READ
    def read(context, destination, count, timeout):
        block = take(incoming, count, timeout / 1000)
        if block: C.memmove(destination, block, len(block))
        return len(block)

    @WRITE
    def write(context, pointer, count):
        block = C.string_at(pointer, count)
        with condition:
            if block == b'\x06' and fault == 'drop-' + state['phase'] + '-ack' and not state['used']:
                state['used'] = True
            else:
                outgoing.extend(block); condition.notify_all()
        return count

    @WRITE
    def store(context, pointer, count):
        stored.extend(C.string_at(pointer, count)); return count

    @PENDING
    def pending(context):
        deadline = time.monotonic() + 1.5
        with condition:
            while not incoming and time.monotonic() < deadline:
                condition.wait(max(0, deadline - time.monotonic()))
            return int(not incoming or incoming[0] == ord('{'))

    class Connection:
        @property
        def in_waiting(self):
            with condition: return len(outgoing)

        def read(self, count): return take(outgoing, count, 0.005)

        def write(self, block):
            with condition:
                if len(block) in (133, 1029):
                    state['phase'] = 'trailer' if block[1] == 0 and not any(block[3:-2]) else ('header' if block[1] == 0 and not stored else 'data')
                    if fault == 'crc' and state['phase'] == 'data' and not state['used']:
                        changed = bytearray(block); changed[-1] ^= 1; block = bytes(changed); state['used'] = True
                incoming.extend(block); condition.notify_all()
            return len(block)

    io = IO(None, read, write, store, pending, size)
    def receive(): state['result'] = lib.Ymodem_Receive(C.byref(io))
    thread = threading.Thread(target=receive, daemon=True); thread.start()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'test.bin'; path.write_bytes(data)
        client = TfClient(Connection())
        client._send_ymodem(path)
        command = b'{"v":2,"cmd":"tf","id":1,"op":"finish"}\n'
        client.connection.write(command)
        thread.join(5)
        assert not thread.is_alive(), (size, fault, 'receiver did not return')
        assert state['result'] == size and stored == data, (size, fault, state)
        assert incoming == command, (size, fault, bytes(incoming[:50]))
        assert not outgoing and not client.pending, (size, fault, bytes(outgoing), bytes(client.pending))
        if fault: assert state['used'], fault
    print(f'PASS interop: {size} bytes; {fault or "normal"}', flush=True)


def main():
    lib = compile_receiver()
    for size in (0, 1, 1023, 1024, 1025, 300 * 1024 + 17): transfer(lib, size)
    for fault in ('crc', 'drop-header-ack', 'drop-data-ack', 'drop-trailer-ack'):
        transfer(lib, 65539, fault)


if __name__ == '__main__': main()
