"""Codex Desktop approvals over its local Windows pipe.

IPC framing/state patches and follower methods adapted from codex-mobile,
commit f79e6807ca0b9d6052afd24f822ee41b9a52e07d (MIT); see LICENSE-codex-mobile.
This is Desktop IPC, not a second app-server or keyboard automation.
"""

import copy
import ctypes
import json
import os
import queue
import struct
import threading
import time
import uuid


MAX_FRAME = 16 * 1024 * 1024
PIPE_PATH = r"\\.\pipe\codex-ipc"
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "thread-follower-command-approval-decision",
    "item/fileChange/requestApproval": "thread-follower-file-approval-decision",
}


class WindowsPipe:
    """Bounded overlapped I/O using the Win32 bindings already in pySerial.

    One worker owns the pipe. A timed-out read stays pending until the next
    poll; closing cancels and drains it before freeing its buffer/OVERLAPPED.
    """

    def __init__(self, path=PIPE_PATH):
        from serial import win32
        self.api = w = win32
        self.handle = w.CreateFile(path, w.GENERIC_READ | w.GENERIC_WRITE, 0, None,
                                   w.OPEN_EXISTING, w.FILE_FLAG_OVERLAPPED, None)
        if self.handle == w.INVALID_HANDLE_VALUE:
            raise ctypes.WinError(w.GetLastError())
        self.read_op = w.OVERLAPPED()
        self.read_op.hEvent = w.CreateEvent(None, True, False, None)
        self.read_buffer = ctypes.create_string_buffer(65536)
        self.read_pending = False
        if not self.read_op.hEvent:
            self.close()
            raise OSError("Cannot create Codex pipe read event")

    def read(self, timeout=0.1):
        w = self.api
        count = w.DWORD()
        if not self.read_pending:
            w.ResetEvent(self.read_op.hEvent)
            if w.ReadFile(self.handle, self.read_buffer, len(self.read_buffer),
                          ctypes.byref(count), ctypes.byref(self.read_op)):
                if not count.value:
                    raise OSError("Codex pipe closed")
                return self.read_buffer.raw[:count.value]
            if w.GetLastError() != w.ERROR_IO_PENDING:
                raise ctypes.WinError(w.GetLastError())
            self.read_pending = True
        result = w.WaitForSingleObject(self.read_op.hEvent, int(timeout * 1000))
        if result == 258:  # WAIT_TIMEOUT
            return b""
        if result != 0:
            raise OSError("Codex pipe read wait failed")
        self.read_pending = False
        if not w.GetOverlappedResult(self.handle, ctypes.byref(self.read_op), ctypes.byref(count), False):
            raise ctypes.WinError(w.GetLastError())
        if not count.value:
            raise OSError("Codex pipe closed")
        return self.read_buffer.raw[:count.value]

    def write(self, data):
        w = self.api
        op = w.OVERLAPPED()
        op.hEvent = w.CreateEvent(None, True, False, None)
        if not op.hEvent:
            raise OSError("Cannot create Codex pipe write event")
        buffer = ctypes.create_string_buffer(data)
        count = w.DWORD()
        try:
            if not w.WriteFile(self.handle, buffer, len(data), ctypes.byref(count), ctypes.byref(op)):
                if w.GetLastError() != w.ERROR_IO_PENDING:
                    raise ctypes.WinError(w.GetLastError())
                if w.WaitForSingleObject(op.hEvent, 2000) != 0:
                    w.CancelIoEx(self.handle, ctypes.byref(op))
                    w.GetOverlappedResult(self.handle, ctypes.byref(op), ctypes.byref(count), True)
                    raise OSError("Codex pipe write timed out")
                if not w.GetOverlappedResult(self.handle, ctypes.byref(op), ctypes.byref(count), False):
                    raise ctypes.WinError(w.GetLastError())
            if count.value != len(data):
                raise OSError("Incomplete Codex pipe write")
        finally:
            w.CloseHandle(op.hEvent)

    def close(self):
        w = self.api
        if self.handle != w.INVALID_HANDLE_VALUE:
            if self.read_pending:
                w.CancelIoEx(self.handle, ctypes.byref(self.read_op))
                count = w.DWORD()
                w.GetOverlappedResult(self.handle, ctypes.byref(self.read_op), ctypes.byref(count), True)
            w.CloseHandle(self.handle)
            self.handle = w.INVALID_HANDLE_VALUE
        if self.read_op.hEvent:
            w.CloseHandle(self.read_op.hEvent)
            self.read_op.hEvent = None


def encode_frame(value):
    body = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if not 0 < len(body) <= MAX_FRAME:
        raise ValueError("Codex IPC frame exceeds limit")
    return struct.pack("<I", len(body)) + body


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 4:
            size = struct.unpack_from("<I", self.buffer)[0]
            if not 0 < size <= MAX_FRAME:
                raise ValueError("Invalid Codex IPC frame size")
            if len(self.buffer) < size + 4:
                break
            value = json.loads(bytes(self.buffer[4:4 + size]))
            del self.buffer[:4 + size]
            if not isinstance(value, dict):
                raise ValueError("Invalid Codex IPC message")
            yield value


def apply_patches(state, patches):
    """Immer-style paths used by Desktop; reject an incomplete/unknown delta."""
    if not isinstance(patches, list) or len(patches) > 4096:
        raise ValueError("Invalid Codex state patches")
    result = copy.deepcopy(state)
    for patch in patches:
        if not isinstance(patch, dict):
            raise ValueError("Invalid Codex state patch")
        path, op = patch.get("path"), patch.get("op")
        if not isinstance(path, list) or not path or len(path) > 64 or op not in ("add", "replace", "remove"):
            raise ValueError("Unsupported Codex state patch")
        parent = result
        for part in path[:-1]:
            if isinstance(parent, dict) and isinstance(part, str) and part in parent:
                parent = parent[part]
            elif isinstance(parent, list) and type(part) is int and 0 <= part < len(parent):
                parent = parent[part]
            else:
                raise ValueError("Codex patch lacks its base state")
        part = path[-1]
        if op != "remove" and "value" not in patch:
            raise ValueError("Codex patch lacks a value")
        if isinstance(parent, dict) and isinstance(part, str):
            if op != "add" and part not in parent:
                raise ValueError("Codex patch target missing")
            if op == "remove":
                del parent[part]
            else:
                parent[part] = copy.deepcopy(patch["value"])
        elif isinstance(parent, list) and type(part) is int and 0 <= part <= len(parent):
            if op == "add":
                parent.insert(part, copy.deepcopy(patch["value"]))
            elif part < len(parent):
                if op == "remove":
                    parent.pop(part)
                else:
                    parent[part] = copy.deepcopy(patch["value"])
            else:
                raise ValueError("Codex patch index missing")
        else:
            raise ValueError("Invalid Codex patch target")
    return result


def ascii_text(value, limit=48):
    return " ".join("".join(ch if 32 <= ord(ch) <= 126 else " " for ch in str(value)).split())[:limit]


def conversation_status(state):
    history = state.get("turnHistory") or {}
    turns = state.get("turns") or []
    if history.get("kind") == "canonical":
        data = history.get("history") or {}
        turns = [data.get("entitiesByKey", {}).get(entry.get("value"), {})
                 for island in data.get("islands", []) for entry in island.get("entries", [])]
    latest = next((turn for turn in reversed(turns) if turn.get("turnId")), {})
    runtime = (state.get("threadRuntimeStatus") or {}).get("type")
    if runtime == "active" or latest.get("status") == "inProgress" or state.get("resumeState") == "resuming":
        return "running"
    if runtime == "systemError" or latest.get("status") == "failed":
        return "error"
    if latest.get("status") == "completed":
        return "done"
    return "idle"


class CodexApprovalAdapter:
    def __init__(self, changed=None, pipe_factory=None, start=True):
        self.changed = changed or threading.Event()
        self._factory = pipe_factory or WindowsPipe
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._commands = queue.Queue(maxsize=1)
        self._client_id = None
        self._states = {}
        self._subscriptions = {}
        self._revisions = {}
        self._requests = {}
        self._pending_rpc = {}
        self._error = ""
        self._thread = None
        if start and (os.name == "nt" or pipe_factory is not None):
            self._thread = threading.Thread(target=self._run, name="xte-codex-approval", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _disconnect(self):
        with self._lock:
            self._client_id = None
            self._states.clear()
            self._subscriptions.clear()
            self._revisions.clear()
            self._requests.clear()
            for command in self._pending_rpc.values():
                command["event"].set()
            self._pending_rpc.clear()
            while True:
                try:
                    self._commands.get_nowait()["event"].set()
                except queue.Empty:
                    break
            self._error = ""
        self.changed.set()

    def snapshot(self):
        with self._lock:
            if not self._client_id:
                return {"tool": "codex", "state": "offline", "id": "", "text": "Desktop IPC unavailable"}
            if self._error:
                return {"tool": "codex", "state": "error", "id": "", "text": self._error}
            for request in self._requests.values():
                if not request["sent"]:
                    return {"tool": "codex", "state": "ask", "id": request["token"], "text": request["text"]}
            if any(request["sent"] for request in self._requests.values()):
                text, state = "Decision sent; waiting for Codex", "running"
            elif any(request.get("method") in (*APPROVAL_METHODS, "item/tool/requestUserInput", "item/permissions/requestApproval",
                                              "mcpServer/elicitation/request", "item/tool/requestOptionPicker")
                     for value in self._states.values() for request in value.get("requests", [])):
                text, state = "Input required in Codex on PC", "idle"
            else:
                text, state = "Desktop connected", "idle"
                states = [conversation_status(value) for value in reversed(list(self._states.values()))]
                for candidate in ("running", "error", "done"):
                    if candidate in states:
                        state = candidate
                        text = {"running": "Working", "error": "Check Codex on PC", "done": "Turn completed"}[state]
                        break
            return {"tool": "codex", "state": state, "id": "", "text": text}

    def decide(self, token, action):
        if action not in ("approve", "reject"):
            return False
        with self._lock:
            current = next((r for r in self._requests.values() if not r["sent"]), None)
            if not self._client_id or self._error or not current or current["token"] != token:
                return False
            command = {"token": token, "action": action, "event": threading.Event(), "ok": False}
            try:
                self._commands.put_nowait(command)
            except queue.Full:
                return False
            current["sent"] = True
        self.changed.set()
        command["event"].wait(3)
        return command["ok"]

    def _update_requests(self, thread_id, state, owner):
        raw_requests = state.get("requests", [])
        if not isinstance(raw_requests, list) or len(raw_requests) > 256:
            raise ValueError("Invalid Codex pending requests")
        previous = {key: value for key, value in self._requests.items() if key[0] == thread_id}
        seen = set()
        for raw in raw_requests:
            if not isinstance(raw, dict) or raw.get("method") not in APPROVAL_METHODS:
                continue
            rpc_id, params = raw.get("id"), raw.get("params")
            if type(rpc_id) not in (int, str) or (isinstance(rpc_id, str) and not 0 < len(rpc_id) <= 256) or not isinstance(params, dict):
                continue
            if params.get("threadId", thread_id) != thread_id or not isinstance(params.get("turnId"), str) or not params["turnId"]:
                continue
            options = params.get("availableDecisions")
            reject_decision = "decline"
            if options is not None:
                if not isinstance(options, list) or "accept" not in options:
                    continue
                if "decline" in options:
                    reject_decision = "decline"
                elif "cancel" in options:
                    reject_decision = "cancel"
                else:
                    continue
            key = (thread_id, type(rpc_id).__name__, rpc_id)
            if key in seen:
                raise ValueError("Duplicate Codex request identity")
            seen.add(key)
            prior = previous.get(key)
            if prior and prior["raw"] == raw and prior["owner"] == owner:
                self._requests[key] = prior
                continue
            kind = "CMD" if raw["method"] == "item/commandExecution/requestApproval" else "FILE"
            detail = params.get("command") or params.get("reason") or "Review on PC"
            if isinstance(detail, list):
                detail = " ".join(str(part) for part in detail)
            self._requests[key] = {"raw": copy.deepcopy(raw), "owner": owner, "thread": thread_id,
                                   "reject_decision": reject_decision,
                                   "token": uuid.uuid4().hex, "sent": False,
                                   "text": ascii_text(f"{kind} {thread_id[:6]}: {detail}")}
        for key in previous.keys() - seen:
            del self._requests[key]
        if self._error and not any(request["sent"] for request in self._requests.values()):
            self._error = ""

    def _handle(self, frame, pipe):
        kind = frame.get("type")
        if kind == "client-discovery-request":
            if self._client_id and isinstance(frame.get("requestId"), str):
                pipe.write(encode_frame({"type": "client-discovery-response", "requestId": frame["requestId"],
                                         "sourceClientId": self._client_id, "version": 1,
                                         "result": {"canHandle": False}}))
            return
        if kind == "response":
            if frame.get("requestId") == "init":
                result = frame.get("result")
                if frame.get("resultType") != "success" or not isinstance(result, dict) or not isinstance(result.get("clientId"), str) or not result["clientId"]:
                    raise ValueError("Codex Desktop rejected initialization")
                with self._lock:
                    self._client_id = result["clientId"]
                print("Codex Desktop IPC connected")
                self.changed.set()
                return
            with self._lock:
                command = self._pending_rpc.pop(frame.get("requestId"), None)
                if command:
                    command["ok"] = frame.get("resultType") == "success"
                    if not command["ok"]:
                        self._error = "Approval failed; check Codex on PC"
                    command["event"].set()
                    self.changed.set()
            return
        if kind != "broadcast":
            return
        targets = frame.get("targetClientIds")
        if targets is not None and (not isinstance(targets, list) or self._client_id not in targets):
            return
        method = frame.get("method")
        params = frame.get("params")
        if not isinstance(params, dict):
            raise ValueError("Invalid Codex state event")
        if method == "ipc-connection-reset":
            raise OSError("Codex Desktop IPC reset")
        if method == "client-status-changed":
            if params.get("status") == "disconnected":
                with self._lock:
                    for thread_id, (_, owner) in list(self._subscriptions.items()):
                        if owner == params.get("clientId"):
                            self._forget_thread(thread_id)
            return
        if method == "thread-stream-following-changed":
            thread_id, host, owner = params.get("conversationId"), params.get("hostId"), frame.get("sourceClientId")
            if not isinstance(thread_id, str) or not isinstance(host, str) or not isinstance(owner, str) or owner == self._client_id:
                return
            with self._lock:
                if params.get("following") is True:
                    subscription = (host, owner)
                    if self._subscriptions.get(thread_id) != subscription:
                        self._forget_thread(thread_id)
                        self._subscriptions[thread_id] = subscription
                        self._subscribe(thread_id, pipe)
                elif params.get("following") is False and self._subscriptions.get(thread_id) == (host, owner):
                    self._forget_thread(thread_id)
            return
        if method != "thread-stream-state-changed":
            return
        if frame.get("version", 11) != 11:
            raise ValueError("Unsupported Codex Desktop state protocol")
        thread_id, change, owner = params.get("conversationId"), params.get("change"), frame.get("sourceClientId")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(change, dict) or not isinstance(owner, str):
            raise ValueError("Invalid Codex conversation identity")
        with self._lock:
            revision = change.get("revision")
            if type(revision) is not int or revision < 0:
                raise ValueError("Invalid Codex state revision")
            if change.get("type") == "snapshot":
                if revision < self._revisions.get(thread_id, -1):
                    return
                state = change.get("conversationState")
                if not isinstance(state, dict):
                    raise ValueError("Invalid Codex state snapshot")
                state = copy.deepcopy(state)
            elif change.get("type") == "patches":
                if thread_id not in self._states:
                    return  # Never infer actionable requests from a delta without its snapshot.
                if self._revisions.get(thread_id) != change.get("baseRevision"):
                    subscription = self._subscriptions.get(thread_id)
                    self._forget_thread(thread_id)
                    if subscription:
                        self._subscriptions[thread_id] = subscription
                        self._subscribe(thread_id, pipe)
                    return
                if revision <= change["baseRevision"]:
                    raise ValueError("Codex state revision did not advance")
                state = apply_patches(self._states[thread_id], change.get("patches"))
            else:
                raise ValueError("Unsupported Codex state change")
            subscription = self._subscriptions.get(thread_id)
            if subscription and subscription[1] != owner:
                return
            if thread_id not in self._states and len(self._states) >= 128:
                raise ValueError("Too many Codex conversations")
            if thread_id not in self._states:
                print("Codex Desktop state synchronized")
            self._update_requests(thread_id, state, owner)
            self._states[thread_id] = state
            self._revisions[thread_id] = revision
        self.changed.set()

    def _forget_thread(self, thread_id):
        self._states.pop(thread_id, None)
        self._subscriptions.pop(thread_id, None)
        self._revisions.pop(thread_id, None)
        for key in list(self._requests):
            if key[0] == thread_id:
                del self._requests[key]
        self.changed.set()

    def _subscribe(self, thread_id, pipe):
        host, owner = self._subscriptions[thread_id]
        pipe.write(encode_frame({"type": "broadcast", "method": "thread-stream-following-changed",
                                 "version": 1, "sourceClientId": self._client_id, "targetClientIds": [owner],
                                 "params": {"conversationId": thread_id, "hostId": host, "following": True}}))

    def _send_command(self, command, pipe):
        with self._lock:
            request = next((r for r in self._requests.values() if r["token"] == command["token"]), None)
            if not self._client_id or not request or self._error:
                command["event"].set()
                return
            raw = request["raw"]
            rpc_id = uuid.uuid4().hex
            payload = {"type": "request", "requestId": rpc_id, "sourceClientId": self._client_id,
                       "version": 1, "method": APPROVAL_METHODS[raw["method"]],
                       "targetClientId": request["owner"],
                       "params": {"conversationId": request["thread"],
                                  "requestId": raw["id"],
                                  "decision": "accept" if command["action"] == "approve" else request["reject_decision"]}}
            command["deadline"] = time.monotonic() + 2
            self._pending_rpc[rpc_id] = command
            pipe.write(encode_frame(payload))

    def _run(self):
        while not self._stop.is_set():
            pipe = None
            try:
                pipe = self._factory()
                decoder = FrameDecoder()
                pipe.write(encode_frame({"type": "request", "requestId": "init", "sourceClientId": "initializing-client",
                                         "version": 1, "method": "initialize", "params": {"clientType": "xte-link"}}))
                initialize_deadline = time.monotonic() + 3
                while not self._stop.is_set():
                    data = pipe.read(0.1)
                    for frame in decoder.feed(data):
                        self._handle(frame, pipe)
                    with self._lock:
                        if not self._client_id and time.monotonic() >= initialize_deadline:
                            raise OSError("Codex initialization timed out")
                        for rpc_id, command in list(self._pending_rpc.items()):
                            if time.monotonic() >= command["deadline"]:
                                del self._pending_rpc[rpc_id]
                                self._error = "Approval unconfirmed; check Codex on PC"
                                command["event"].set()
                                self.changed.set()
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        continue
                    self._send_command(command, pipe)
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                # Don't log commands, conversation content, or credentials.
                if self._client_id:
                    print(f"Codex Desktop IPC disconnected: {type(exc).__name__}")
            finally:
                self._disconnect()
                if pipe is not None:
                    pipe.close()
            self._stop.wait(2)
