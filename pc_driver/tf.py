"""Read and write TF-card files over the XTE-Link USB serial connection."""

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time

import serial

MAX_PAYLOAD = 384
CHUNK = 128
PREFIX = b"XTE: tf "


def remote_path(value):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 96:
        raise ValueError("TF paths must be absolute and at most 96 characters")
    if any(ord(ch) < 32 or ch in '\\:"*?<>|' for ch in value):
        raise ValueError("Unsupported character in TF path")
    if "//" in value or any(p in (".", "..") for p in value.split("/")):
        raise ValueError("TF path must be canonical")
    if value != "/" and value.endswith("/"):
        raise ValueError("Remove the trailing slash from the TF path")
    return value


class TfClient:
    def __init__(self, connection, timeout=45):
        self.connection = connection
        self.timeout = timeout
        self.sequence = secrets.randbelow(0x7fffffff)

    def request(self, op, **fields):
        self.sequence = (self.sequence + 1) & 0xffffffff
        payload = {"v": 2, "cmd": "tf", "id": self.sequence, "op": op, **fields}
        frame = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(frame) > MAX_PAYLOAD:
            raise ValueError("TF request exceeds firmware frame limit")
        if self.connection.write(frame + b"\n") != len(frame) + 1:
            raise OSError("Incomplete serial write")
        deadline = time.monotonic() + self.timeout
        line = bytearray()
        overflow = False
        while time.monotonic() < deadline:
            byte = self.connection.read(1)
            if not byte:
                continue
            if byte != b"\n":
                if len(line) < 2048 and not overflow:
                    line.extend(byte)
                else:
                    overflow = True
                continue
            raw = bytes(line)
            line.clear()
            if overflow:
                overflow = False
                continue
            if not raw.startswith(PREFIX):
                continue
            try:
                response = json.loads(raw[len(PREFIX):])
            except (ValueError, UnicodeError) as exc:
                raise OSError("Malformed TF response") from exc
            if not isinstance(response, dict) or response.get("id") != self.sequence:
                continue
            if response.get("ok") is not True:
                raise OSError(f"TF {op}: {response.get('error', 'invalid_response')}")
            return response
        raise TimeoutError(f"TF {op} timed out; check firmware, USB connection and card")

    def mount(self):
        response = self.request("mount")
        if response.get("ready") is not True or response.get("chunk") != CHUNK:
            raise OSError("Incompatible TF firmware")

    def list(self, path="/"):
        self.request("list", path=remote_path(path))
        while True:
            entry = self.request("list_next")
            if entry.get("eof") is True:
                return
            if not isinstance(entry.get("name"), str) or type(entry.get("dir")) is not bool:
                raise OSError("Invalid directory response")
            yield entry

    def upload(self, local, remote):
        remote_path(remote)
        if ".xte-part" in remote:
            raise ValueError(".xte-part is reserved for incomplete uploads")
        with Path(local).open("rb") as source:
            digest = hashlib.sha256()
            size = 0
            while block := source.read(65536):
                digest.update(block)
                size += len(block)
            if size > 0xffffffff:
                raise ValueError("File exceeds the 4 GiB TF protocol limit")
            expected = digest.hexdigest()
            source.seek(0)
            self.request("put", path=remote, size=size, sha256=expected)
            offset = 0
            while block := source.read(CHUNK):
                response = self.request("write", offset=offset, data=block.hex())
                offset += len(block)
                if response.get("offset") != offset:
                    raise OSError("Unexpected upload offset")
            response = self.request("finish")
            if response.get("sha256") != expected:
                raise OSError("Upload SHA-256 mismatch")
        return size, expected

    def download(self, remote, local):
        remote_path(remote)
        destination = Path(local)
        if destination.exists():
            raise FileExistsError(f"Local destination already exists: {destination}")
        temporary = destination.with_name(destination.name + ".xte-part")
        created = False
        try:
            with temporary.open("xb") as output:
                created = True
                response = self.request("get", path=remote)
                size = response.get("size")
                if type(size) is not int or not 0 <= size <= 0xffffffff:
                    raise OSError("Invalid download size")
                digest = hashlib.sha256()
                offset = 0
                while offset < size:
                    response = self.request("read", offset=offset)
                    data = response.get("data")
                    want = min(CHUNK, size - offset)
                    if not isinstance(data, str) or len(data) != want * 2:
                        raise OSError("Invalid download chunk length")
                    try:
                        block = bytes.fromhex(data)
                    except ValueError as exc:
                        raise OSError("Invalid download hex") from exc
                    if len(block) != want or response.get("offset") != offset + want:
                        raise OSError("Unexpected download offset")
                    output.write(block)
                    digest.update(block)
                    offset += want
                expected = digest.hexdigest()
                if self.request("finish").get("sha256") != expected:
                    raise OSError("Download SHA-256 mismatch")
                output.flush()
            if destination.exists():
                raise FileExistsError(f"Local destination already exists: {destination}")
            temporary.rename(destination)
            return size, expected
        finally:
            if created and temporary.exists():
                temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="USB COM port; close other users of this port first")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mount")
    listing = commands.add_parser("ls")
    listing.add_argument("path", nargs="?", default="/")
    for name in ("mkdir", "rm"):
        commands.add_parser(name).add_argument("path")
    commands.add_parser("format", help="Format the TF card; destroys all files")
    upload = commands.add_parser("put")
    upload.add_argument("local")
    upload.add_argument("remote")
    download = commands.add_parser("get")
    download.add_argument("remote")
    download.add_argument("local")
    commands.add_parser("abort", help="Cancel a stale session and remove its unfinished upload")
    args = parser.parse_args(argv)
    connection = None
    client = None
    try:
        connection = serial.serial_for_url(args.port, baudrate=115200, timeout=0.1,
                                           write_timeout=5, do_not_open=True)
        connection.dtr = False
        connection.rts = False
        connection.open()
        client = TfClient(connection)
        if args.command == "abort":
            client.request("abort")
        elif args.command == "format":
            client.mount()
            client.request("format")
            print("TF formatted")
        else:
            client.mount()
            if args.command == "ls":
                for entry in client.list(args.path):
                    print(f"{'DIR' if entry['dir'] else entry.get('size', '?'):>10}  {entry['name']}")
            elif args.command in ("put", "get"):
                size, digest = (client.upload(args.local, args.remote) if args.command == "put"
                                else client.download(args.remote, args.local))
                print(f"Verified {size} bytes, SHA-256 {digest}")
            elif args.command in ("mkdir", "rm"):
                client.request("mkdir" if args.command == "mkdir" else "remove", path=remote_path(args.path))
            else:
                print("TF mounted")
        return 0
    except (OSError, ValueError, serial.SerialException, KeyboardInterrupt) as exc:
        print(f"TF error: {exc}", file=sys.stderr)
        if client is not None and connection.is_open:
            try:
                client.timeout = 3
                client.request("abort")
            except (OSError, ValueError, serial.SerialException):
                pass
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
