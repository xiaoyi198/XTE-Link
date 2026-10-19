import json
import os
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from pc_driver.codex_ipc import (APPROVAL_METHODS, MAX_FRAME, CodexApprovalAdapter,
                                FrameDecoder, WindowsPipe, apply_patches, encode_frame)
from pc_driver.driver import AgentManager, agent_confirmation, demo_status


def approval(rpc_id=7, thread="chat-a", method="item/commandExecution/requestApproval", **params):
    return {"id": rpc_id, "method": method,
            "params": {"threadId": thread, "turnId": "turn-a", "itemId": "item-a",
                       "command": "echo XTE approval test", **params}}


def snapshot(requests, thread="chat-a", owner="desktop-a"):
    return {"type": "broadcast", "method": "thread-stream-state-changed", "sourceClientId": owner,
            "params": {"conversationId": thread, "change": {"type": "snapshot",
                       "revision": 0, "conversationState": {"requests": requests, "isRunning": True}}}}


class FakePipe:
    def __init__(self, requests=None, result="success"):
        self.incoming = queue.Queue()
        self.frames = []
        self.requests = [approval()] if requests is None else requests
        self.result = result
        self.closed = False

    def write(self, data):
        frame = next(FrameDecoder().feed(data))
        self.frames.append(frame)
        if frame.get("method") == "initialize":
            self.incoming.put(encode_frame({"type": "response", "requestId": "init",
                                           "resultType": "success", "result": {"clientId": "xte-client"}}))
            self.incoming.put(encode_frame(snapshot(self.requests)))
        elif frame.get("method") in APPROVAL_METHODS.values() and self.result is not None:
            self.incoming.put(encode_frame({"type": "response", "requestId": frame["requestId"],
                                           "resultType": self.result, "result": None}))

    def read(self, timeout=0.1):
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class CodexIpcTests(unittest.TestCase):
    def adapter(self):
        adapter = CodexApprovalAdapter(start=False)
        adapter._client_id = "xte-client"
        return adapter

    def test_fragmented_frames_and_invalid_lengths(self):
        decoder = FrameDecoder()
        data = encode_frame(snapshot([approval()])) + encode_frame({"type": "response"})
        frames = []
        for byte in data:
            frames.extend(decoder.feed(bytes([byte])))
        self.assertEqual(len(frames), 2)
        for size in (0, MAX_FRAME + 1):
            with self.assertRaises(ValueError):
                list(FrameDecoder().feed(struct.pack("<I", size)))

    def test_patches_are_atomic_and_support_array_insertion_and_removal(self):
        base = {"requests": [approval()], "nested": {"count": 1}}
        patches = [{"op": "add", "path": ["requests", 0], "value": approval(8)},
                   {"op": "remove", "path": ["requests", 1]},
                   {"op": "replace", "path": ["nested", "count"], "value": 2}]
        result = apply_patches(base, patches)
        self.assertEqual([r["id"] for r in result["requests"]], [8])
        self.assertEqual(base["nested"]["count"], 1)
        with self.assertRaises(ValueError):
            apply_patches(base, patches + [{"op": "replace", "path": ["absent"], "value": 0}])
        self.assertEqual(base["requests"][0]["id"], 7)

    def test_request_tokens_survive_unrelated_changes_but_not_modified_approvals(self):
        adapter = self.adapter()
        event = snapshot([approval()])
        adapter._handle(event, None)
        token = adapter.snapshot()["id"]
        self.assertTrue(token)
        event["params"]["change"]["conversationState"]["isRunning"] = False
        adapter._handle(event, None)
        self.assertEqual(adapter.snapshot()["id"], token)
        event["params"]["change"]["conversationState"]["requests"][0]["params"]["command"] = "different command"
        adapter._handle(event, None)
        self.assertNotEqual(adapter.snapshot()["id"], token)
        self.assertFalse(adapter.decide(token, "approve"))

    def test_removal_and_disconnect_invalidate_old_device_commands(self):
        adapter = self.adapter()
        adapter._handle(snapshot([approval()]), None)
        token = adapter.snapshot()["id"]
        adapter._handle(snapshot([]), None)
        self.assertFalse(adapter.decide(token, "approve"))
        adapter._handle(snapshot([approval()]), None)
        replacement = adapter.snapshot()["id"]
        self.assertNotEqual(replacement, token)
        adapter._disconnect()
        self.assertFalse(adapter.decide(replacement, "approve"))
        self.assertEqual(adapter.snapshot()["state"], "offline")

    def test_delta_without_snapshot_and_unsupported_questions_cannot_be_approved(self):
        adapter = self.adapter()
        event = snapshot([])
        event["params"]["change"] = {"type": "patches", "baseRevision": 0, "revision": 1, "patches": [
            {"op": "add", "path": ["requests", 0], "value": approval()}]}
        adapter._handle(event, None)
        self.assertNotEqual(adapter.snapshot()["state"], "ask")
        for request in (approval(method="item/tool/requestUserInput"),
                        approval(availableDecisions=["acceptForSession", "cancel"]),
                        approval(turnId=None), approval(threadId="wrong-chat")):
            adapter._handle(snapshot([request]), None)
            self.assertNotEqual(adapter.snapshot()["state"], "ask")

    def test_multiple_chats_and_typed_request_ids_are_isolated(self):
        adapter = self.adapter()
        adapter._handle(snapshot([approval(7), approval("7")]), None)
        adapter._handle(snapshot([approval(7, thread="chat-b")], thread="chat-b", owner="desktop-b"), None)
        self.assertEqual(len(adapter._requests), 3)
        first = adapter.snapshot()["id"]
        adapter._handle(snapshot([approval(7, thread="chat-b")], thread="chat-b", owner="desktop-b"), None)
        self.assertEqual(adapter.snapshot()["id"], first)
        adapter._handle(snapshot([approval("7")]), None)
        next_request = next(r for r in adapter._requests.values() if r["token"] == adapter.snapshot()["id"])
        self.assertNotEqual(adapter.snapshot()["id"], first)
        self.assertIn(next_request["raw"]["params"]["threadId"], ("chat-a", "chat-b"))

    def test_subscription_and_revision_gap_refresh_invalidate_old_approval(self):
        adapter = self.adapter()
        pipe = FakePipe()
        following = {"type": "broadcast", "method": "thread-stream-following-changed", "sourceClientId": "desktop-a",
                     "params": {"conversationId": "chat-a", "hostId": "local", "following": True}}
        adapter._handle(following, pipe)
        self.assertEqual(pipe.frames[-1]["params"]["following"], True)
        adapter._handle(snapshot([approval()]), pipe)
        token = adapter.snapshot()["id"]
        event = snapshot([])
        event["params"]["change"] = {"type": "patches", "baseRevision": 4, "revision": 5, "patches": []}
        adapter._handle(event, pipe)
        self.assertFalse(adapter.decide(token, "approve"))
        self.assertEqual(len(pipe.frames), 2)
        adapter._handle(snapshot([approval()]), pipe)
        self.assertNotEqual(adapter.snapshot()["id"], token)
        adapter._handle({"type": "broadcast", "method": "client-status-changed",
                         "params": {"clientId": "desktop-a", "status": "disconnected"}}, pipe)
        self.assertEqual(adapter._requests, {})

    def test_actual_canonical_turn_status_and_ordered_request_removal(self):
        adapter = self.adapter()
        event = snapshot([approval()])
        state = event["params"]["change"]["conversationState"]
        state["turnHistory"] = {"kind": "canonical", "history": {
            "islands": [{"entries": [{"value": "turn-key"}]}],
            "entitiesByKey": {"turn-key": {"turnId": "turn-a", "status": "inProgress"}}}}
        adapter._handle(event, None)
        event["params"]["change"] = {"type": "patches", "baseRevision": 0, "revision": 1, "patches": [
            {"op": "replace", "path": ["requests"], "value": []}]}
        adapter._handle(event, None)
        self.assertEqual(adapter.snapshot()["state"], "running")
        event["params"]["change"] = {"type": "patches", "baseRevision": 1, "revision": 2, "patches": [
            {"op": "replace", "path": ["turnHistory", "history", "entitiesByKey", "turn-key", "status"], "value": "completed"}]}
        adapter._handle(event, None)
        self.assertEqual(adapter.snapshot()["state"], "done")

    def test_old_snapshot_cannot_restore_resolved_approval(self):
        adapter = self.adapter()
        old = snapshot([approval()])
        adapter._handle(old, None)
        token = adapter.snapshot()["id"]
        resolved = snapshot([])
        resolved["params"]["change"]["revision"] = 1
        adapter._handle(resolved, None)
        adapter._handle(old, None)
        self.assertFalse(adapter.decide(token, "approve"))
        self.assertNotEqual(adapter.snapshot()["state"], "ask")
        resolved["params"]["change"] = {"type": "patches", "baseRevision": 1, "revision": 0, "patches": []}
        with self.assertRaises(ValueError):
            adapter._handle(resolved, None)

    def test_real_worker_routes_one_time_approve_and_reject_and_waits_for_resolution(self):
        for action, decision, method in (("approve", "accept", "item/commandExecution/requestApproval"),
                                         ("reject", "decline", "item/fileChange/requestApproval")):
            with self.subTest(action=action):
                pipe = FakePipe([approval("007", method=method)])
                adapter = CodexApprovalAdapter(pipe_factory=lambda: pipe)
                try:
                    self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask"))
                    token = adapter.snapshot()["id"]
                    self.assertTrue(adapter.decide(token, action))
                    self.assertFalse(adapter.decide(token, action))
                    writes = [f for f in pipe.frames if f.get("method") in APPROVAL_METHODS.values()]
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0]["targetClientId"], "desktop-a")
                    self.assertEqual(writes[0]["params"], {"conversationId": "chat-a", "requestId": "007", "decision": decision})
                    self.assertEqual(writes[0]["method"], APPROVAL_METHODS[method])
                    self.assertEqual(adapter.snapshot()["state"], "running")
                    pipe.incoming.put(encode_frame(snapshot([])))
                    self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "idle"))
                finally:
                    adapter.close()
                self.assertTrue(pipe.closed)
                self.assertFalse(adapter._thread.is_alive())

    def test_native_failure_and_lost_ack_do_not_report_done_or_repeat(self):
        for result in ("error", None):
            with self.subTest(result=result):
                pipe = FakePipe(result=result)
                adapter = CodexApprovalAdapter(pipe_factory=lambda: pipe)
                try:
                    self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask"))
                    token = adapter.snapshot()["id"]
                    self.assertFalse(adapter.decide(token, "approve"))
                    self.assertEqual(adapter.snapshot()["state"], "error")
                    self.assertFalse(adapter.decide(token, "approve"))
                    self.assertEqual(len([f for f in pipe.frames if f.get("method") in APPROVAL_METHODS.values()]), 1)
                    pipe.incoming.put(encode_frame(snapshot([])))
                    self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "idle"))
                finally:
                    adapter.close()

    def test_captured_desktop_command_with_cancel_instead_of_decline(self):
        # Observed on Desktop 26.1013.4894.0 during the user's manual approval.
        decisions = ["accept", {"acceptWithExecpolicyAmendment": {
            "execpolicy_amendment": ["Write-Output", "XTE-Link test"]}}, "cancel"]
        for action, expected in (("approve", "accept"), ("reject", "cancel")):
            with self.subTest(action=action):
                pipe = FakePipe([approval(10, availableDecisions=decisions)])
                adapter = CodexApprovalAdapter(pipe_factory=lambda: pipe)
                try:
                    self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask"))
                    self.assertTrue(adapter.decide(adapter.snapshot()["id"], action))
                    forwarded = [f for f in pipe.frames if f.get("method") in APPROVAL_METHODS.values()]
                    self.assertEqual(len(forwarded), 1)
                    self.assertEqual(forwarded[0]["params"]["decision"], expected)
                finally:
                    adapter.close()

    def test_unsupported_approval_options_show_pc_input_instead_of_running(self):
        adapter = self.adapter()
        adapter._handle(snapshot([approval(availableDecisions=["acceptForSession", "cancel"])]), None)
        self.assertEqual(adapter.snapshot()["state"], "idle")
        self.assertIn("Input required", adapter.snapshot()["text"])

    def test_queued_decision_is_not_sent_after_desktop_resolves_request(self):
        adapter = self.adapter()
        adapter._handle(snapshot([approval()]), None)
        token = adapter.snapshot()["id"]
        result = []
        caller = threading.Thread(target=lambda: result.append(adapter.decide(token, "approve")))
        caller.start()
        self.assertTrue(wait_until(lambda: not adapter._commands.empty()))
        adapter._handle(snapshot([]), None)
        pipe = FakePipe()
        adapter._send_command(adapter._commands.get_nowait(), pipe)
        caller.join(1)
        self.assertEqual(result, [False])
        self.assertEqual(pipe.frames, [])

    def test_manager_prefers_ipc_and_never_uses_codex_keyboard_fallback(self):
        pipe = FakePipe()
        adapter = CodexApprovalAdapter(pipe_factory=lambda: pipe)
        try:
            self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask"))
            with tempfile.TemporaryDirectory() as root:
                manager = AgentManager(status_path=f"{root}/agents.json", action_path=f"{root}/actions.jsonl", codex_adapter=adapter)
                token = manager.collect()[1]["id"]
                with patch.object(manager, "_desktop_key", side_effect=AssertionError("Keyboard fallback")):
                    self.assertIsNotNone(agent_confirmation(f"XTE: agent_action approve codex {token}", manager, demo_status()))
                    self.assertIsNone(agent_confirmation(f"XTE: agent_action approve codex {token}", manager, demo_status()))
                    adapter._disconnect()
                    self.assertFalse(manager.action("codex", token, "approve"))
                self.assertEqual(manager.collect()[1]["state"], "offline")
                with open(f"{root}/actions.jsonl", encoding="utf-8") as stream:
                    records = stream.readlines()
                self.assertEqual(len(records), 1)
        finally:
            adapter.close()

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "Windows pipe integration")
    def test_windows_pipe_end_to_end_with_isolated_native_server(self):
        name = "xte-codex-test-" + uuid.uuid4().hex
        # Only this unique test pipe is used; this server never contacts Codex.
        script = r'''
$server = [System.IO.Pipes.NamedPipeServerStream]::new('PIPE_NAME', [System.IO.Pipes.PipeDirection]::InOut)
function Read-Bytes([int]$count) {
  $buffer=[byte[]]::new($count); $offset=0
  while ($offset -lt $count) {
    $got=$server.Read($buffer,$offset,$count-$offset)
    if ($got -eq 0) { throw 'Client disconnected' }; $offset += $got
  }
  return ,$buffer
}
function Read-Frame {
  $header=Read-Bytes 4; $length=[BitConverter]::ToUInt32($header,0)
  if ($length -gt 65536) { throw 'Frame exceeds test limit' }
  $body=Read-Bytes $length
  return ([System.Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json)
}
function Write-Frame($value) {
  $bytes=[System.Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Compress -Depth 12))
  $server.Write([BitConverter]::GetBytes([uint32]$bytes.Length),0,4)
  # Fragment across separate writes to exercise the actual stream decoder.
  $server.Write($bytes,0,1); $server.Write($bytes,1,$bytes.Length-1)
}
function Publish-Request($id,$method,$revision) {
  $requests=@()
  if ($null -ne $id) { $requests=@(@{id=$id;method=$method;params=@{threadId='chat-a';turnId='turn-a';command='echo XTE test'}}) }
  Write-Frame @{type='broadcast';method='thread-stream-state-changed';version=11;sourceClientId='desktop-test';params=@{conversationId='chat-a';change=@{type='snapshot';revision=$revision;conversationState=@{requests=$requests}}}}
}
try {
  'READY'
  $server.WaitForConnection()
  $init=Read-Frame
  Write-Frame @{type='response';requestId=$init.requestId;resultType='success';result=@{clientId='xte-test'}}
  Write-Frame @{type='broadcast';method='thread-stream-following-changed';version=1;sourceClientId='desktop-test';params=@{conversationId='chat-a';hostId='local';following=$true}}
  $subscription=Read-Frame
  if ($subscription.type -ne 'broadcast' -or $subscription.params.following -ne $true) { throw 'Subscription missing' }
  Publish-Request 7 'item/commandExecution/requestApproval' 0
  $first=Read-Frame
  Write-Frame @{type='response';requestId=$first.requestId;resultType='success';result=@{ok=$true}}
  Publish-Request '007' 'item/fileChange/requestApproval' 1
  $second=Read-Frame
  Write-Frame @{type='response';requestId=$second.requestId;resultType='success';result=@{ok=$true}}
  Publish-Request $null '' 2
  @($first.params,$second.params) | ConvertTo-Json -Compress
  Start-Sleep -Milliseconds 300
} finally { $server.Dispose() }
'''.replace("PIPE_NAME", name)
        process = subprocess.Popen([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", script],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        adapter = None
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            adapter = CodexApprovalAdapter(pipe_factory=lambda: WindowsPipe("\\\\.\\pipe\\" + name))
            self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask"))
            token = adapter.snapshot()["id"]
            self.assertTrue(adapter.decide(token, "approve"))
            self.assertTrue(wait_until(lambda: adapter.snapshot()["state"] == "ask" and adapter.snapshot()["id"] != token))
            self.assertFalse(adapter.decide(token, "approve"))
            self.assertTrue(adapter.decide(adapter.snapshot()["id"], "reject"))
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(json.loads(output), [
                {"conversationId": "chat-a", "requestId": 7, "decision": "accept"},
                {"conversationId": "chat-a", "requestId": "007", "decision": "decline"}])
        finally:
            if adapter:
                adapter.close()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
