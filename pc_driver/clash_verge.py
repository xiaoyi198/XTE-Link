"""Client for the optional Verge 2.5.8 bridge; read-only unless explicitly enabled.

Only loopback requests with Verge's existing instance token are supported. A command
is posted once; receipt polling never replays it after a timeout or app restart.
"""

from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib import error, request
import uuid

PREFIX = "/xte-link/v1"
MAX_RESPONSE = 2 * 1024 * 1024
OPERATIONS = {"update_profile", "select_node", "set_system_proxy", "set_tun"}


class BridgeUnavailable(OSError):
    pass


class ReadOnlyError(PermissionError):
    pass


class CommandFailed(OSError):
    def __init__(self, code, request_id):
        super().__init__(code)
        self.code, self.request_id = code, request_id


class OutcomeUnknown(CommandFailed):
    """The operation may have applied; callers must read state, not resend it."""


@dataclass(frozen=True)
class Instance:
    port: int
    token: str = field(repr=False)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, record=None, *, allow_mutations=False, timeout=3):
        if type(allow_mutations) is not bool:
            raise ValueError("allow_mutations_must_be_bool")
        self.record = Path(record) if record is not None else (
            Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) /
            "io.github.clash-verge-rev.clash-verge-rev/singleton-instance.json")
        self.allow_mutations = allow_mutations
        self.timeout = timeout
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def _instance(self):
        try:
            with self.record.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            doc = json.loads(raw)
            port, token = doc["port"], doc["token"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError()
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
                raise ValueError()
            return Instance(port, token)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BridgeUnavailable("invalid_or_missing_instance_record") from exc

    def _request(self, instance, method, endpoint, payload=None):
        allowed_read = endpoint in {"/capabilities", "/state"} or bool(
            re.fullmatch(r"/requests/[A-Za-z0-9-]{1,64}", endpoint))
        if not (method == "GET" and allowed_read or method == "POST" and endpoint == "/command"):
            raise ValueError("unsupported_bridge_endpoint")
        if method != "GET" and self.allow_mutations is not True:
            raise ReadOnlyError("clash_control_is_read_only")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if body is not None and len(body) > 4096:
            raise ValueError("command_too_large")
        req = request.Request(f"http://127.0.0.1:{instance.port}{PREFIX}{endpoint}",
                              data=body, method=method,
                              headers={"x-instance-token": instance.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except error.HTTPError as exc:
            raise BridgeUnavailable("bridge_http_%d" % exc.code) from None
        except (OSError, error.URLError, http.client.HTTPException) as exc:
            raise BridgeUnavailable("bridge_connection_failed") from exc
        if len(raw) > MAX_RESPONSE:
            raise BridgeUnavailable("bridge_response_too_large")
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or type(doc.get("v")) is not int or doc["v"] != 1:
                raise ValueError()
        except (ValueError, UnicodeError) as exc:
            raise BridgeUnavailable("invalid_bridge_response") from exc
        return doc

    def capabilities(self):
        return self._capabilities(self._instance())

    def _capabilities(self, instance):
        doc = self._request(instance, "GET", "/capabilities")
        if type(doc.get("ready")) is not bool or not isinstance(doc.get("operations"), list):
            raise BridgeUnavailable("invalid_capabilities")
        return doc

    def state(self):
        doc = self._request(self._instance(), "GET", "/state")
        state = doc.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("profiles"), list) or not isinstance(state.get("groups"), list):
            raise BridgeUnavailable("invalid_state")
        if any(type(state.get(key)) is not bool for key in ("system_proxy", "tun")):
            raise BridgeUnavailable("invalid_state")
        return state

    def execute(self, operation, *, request_id=None, wait_seconds=90):
        if self.allow_mutations is not True:
            raise ReadOnlyError("clash_control_is_read_only")
        if not isinstance(operation, dict) or operation.get("action") not in OPERATIONS:
            raise ValueError("unsupported_operation")
        action = operation["action"]
        fields = {"update_profile": {"uid"}, "select_node": {"group", "node"},
                  "set_system_proxy": {"enabled"}, "set_tun": {"enabled"}}[action]
        if set(operation) != fields | {"action"}:
            raise ValueError("invalid_operation_fields")
        for key in fields:
            value = operation[key]
            if key == "enabled":
                if type(value) is not bool:
                    raise ValueError("enabled_must_be_bool")
            elif not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("invalid_operation_name")
        request_id = uuid.uuid4().hex if request_id is None else request_id
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id):
            raise ValueError("invalid_request_id")
        instance = self._instance()
        caps = self._capabilities(instance)
        if not caps["ready"] or action not in caps["operations"]:
            raise BridgeUnavailable("operation_unavailable")
        payload = {"v": 1, "id": request_id, "operation": operation}
        try:
            receipt = self._request(instance, "POST", "/command", payload)
        except BridgeUnavailable as exc:
            # A rejected request is distinct from a lost response to an accepted write.
            if str(exc) in {"bridge_http_400", "bridge_http_401", "bridge_http_403", "bridge_http_404",
                            "bridge_http_409", "bridge_http_413", "bridge_http_503"}:
                raise
            raise OutcomeUnknown("submission_unconfirmed", request_id) from exc
        deadline = time.monotonic() + wait_seconds
        while True:
            if receipt.get("id") != request_id or receipt.get("status") not in {"pending", "done", "failed"}:
                raise OutcomeUnknown("invalid_receipt", request_id)
            if receipt["status"] == "done":
                return receipt
            if receipt["status"] == "failed":
                problem = receipt.get("error")
                if not isinstance(problem, dict) or type(problem.get("uncertain")) is not bool:
                    raise OutcomeUnknown("invalid_receipt", request_id)
                code = problem.get("code", "operation_failed")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                    code = "operation_failed"
                exception = OutcomeUnknown if problem.get("uncertain") is True else CommandFailed
                raise exception(code, request_id)
            if time.monotonic() >= deadline:
                raise OutcomeUnknown("operation_pending", request_id)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            try:
                if self._instance() != instance:
                    raise BridgeUnavailable("instance_restarted")
                receipt = self._request(instance, "GET", "/requests/" + request_id)
            except BridgeUnavailable as exc:
                raise OutcomeUnknown("receipt_unavailable", request_id) from exc
