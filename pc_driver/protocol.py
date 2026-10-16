"""Version 2 wire format shared with firmware/src/protocol.h."""

import json
import math
import re

MAX_PAYLOAD_BYTES = 384
METRICS = ("cpu", "cpu_temp_c", "gpu", "gpu_temp_c", "mem_used_mib", "mem_total_mib", "volume")
AGENT_TOOLS = ("dsh", "codex")
AGENT_STATES = ("offline", "idle", "running", "ask", "done", "error")


def encode_agent(agent):
    validate_agents([agent])
    payload = json.dumps({"v": 2, "cmd": "agents", "agents": [agent]},
                         ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("agent event exceeds frame limit")
    return payload + b"\n"


def validate_agents(agents):
    if not isinstance(agents, list) or len(agents) > 2:
        raise ValueError("agents must contain at most two entries")
    seen_tools = set()
    for agent in agents:
        if not isinstance(agent, dict):
            raise ValueError("invalid agent entry")
        tool, state = agent.get("tool"), agent.get("state")
        if tool not in AGENT_TOOLS or tool in seen_tools or state not in AGENT_STATES:
            raise ValueError("invalid agent tool or state")
        seen_tools.add(tool)
        for key, limit in (("id", 36), ("text", 48)):
            value = agent.get(key, "")
            if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
                raise ValueError(f"invalid agent {key}")
        if state == "ask" and not agent.get("id"):
            raise ValueError("pending agent request requires id")


def encode_status(status):
    if status.get("v") != 2 or isinstance(status.get("v"), bool):
        raise ValueError("unsupported protocol version")
    host = status.get("host")
    stamp = status.get("time")
    if not isinstance(host, str) or not 1 <= len(host) <= 32:
        raise ValueError("host must contain 1-32 printable ASCII characters")
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in host):
        raise ValueError("host must be printable ASCII")
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", stamp):
        raise ValueError("time must use YYYY-MM-DD HH:MM:SS")
    for key in METRICS:
        value = status.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < (-50 if key.endswith("_temp_c") else 0)
                                  or value > 1_000_000):
            raise ValueError(f"invalid metric: {key}")
    if status.get("cpu") is not None and status["cpu"] > 100:
        raise ValueError("CPU must be in 0-100")
    if status.get("gpu") is not None and status["gpu"] > 100:
        raise ValueError("GPU must be in 0-100")
    if status.get("volume") is not None and status["volume"] > 100:
        raise ValueError("volume must be in 0-100")
    if status.get("cpu_temp_c") is not None and not -50 <= status["cpu_temp_c"] <= 150:
        raise ValueError("CPU temperature must be in -50 to 150 C")
    if status.get("gpu_temp_c") is not None and not -50 <= status["gpu_temp_c"] <= 150:
        raise ValueError("GPU temperature must be in -50 to 150 C")
    used, total = status.get("mem_used_mib"), status.get("mem_total_mib")
    if total is not None and total <= 0:
        raise ValueError("memory total must be positive")
    if used is not None and total is not None and used > total:
        raise ValueError("memory used exceeds total")
    agents = status.get("agents", [])
    validate_agents(agents)
    payload = {key: status.get(key) for key in ("v", "host", "time", *METRICS)}
    if agents:
        payload["agents"] = agents
    if "volume_ack" in status:
        ack = status["volume_ack"]
        if isinstance(ack, bool) or not isinstance(ack, int) or not 0 <= ack <= 0xffffffff:
            raise ValueError("invalid volume acknowledgement")
        payload["volume_ack"] = ack
    line = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(line) > MAX_PAYLOAD_BYTES:
        raise ValueError("status exceeds frame limit")
    return line + b"\n"
