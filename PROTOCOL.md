# XTE-Link serial protocol v2

Transport: USB CDC, monitor configuration 115200, ASCII JSON, one object per LF.
TF file requests use the same framing and a separate `cmd:"tf"` schema with
sequence-correlated responses; see [TF_FILES.md](TF_FILES.md). They do not update
the telemetry offline timer. A TF transfer session pauses screen rendering.
CRLF is accepted if the payload including CR still fits. Maximum payload is
384 bytes excluding LF. No checksum is added for this initial USB verification;
JSON/schema validation is mandatory and messages are bounded.

```json
{"v":2,"host":"DEMO-PC","time":"2026-10-10 12:34:56","cpu":12.5,"gpu":38.0,"gpu_temp_c":54.0,"mem_used_mib":7168,"mem_total_mib":16384,"volume":55.0}
```

| Field | Meaning / limit |
| --- | --- |
| v | Integer 2 |
| host | 1-32 printable ASCII characters |
| time | PC-local sample time, YYYY-MM-DD HH:MM:SS |
| cpu | Number 0-100 or null |
| cpu_temp_c | CPU temperature in Celsius, -50 to 150 or null; accepted for PC schema compatibility and not displayed |
| gpu | GPU utilization 0-100 or null |
| gpu_temp_c | GPU temperature in Celsius, -50 to 150 or null |
| mem_used_mib | Used memory, MiB, 0-1,000,000 or null |
| mem_total_mib | Total memory, MiB, greater than 0 to 1,000,000 or null |
| volume | Optional system volume percentage, 0-100 or null; omitted means unavailable |
| volume_ack | Optional unsigned 32-bit command sequence, echoed for volume confirmation |
| agents | Optional array of at most two desktop tools (`dsh`, `codex`) and their state |

The base status fields are required. `volume` is optional for older drivers;
when present it is a percentage. Memory used may not exceed total when both are known.
Non-finite numbers, wrong types, malformed JSON, unsupported versions and
overlong lines are rejected. An overlong line is discarded through its LF;
the next frame can recover. Rejected frames do not reset the offline timer.
Unknown fields may be ignored within the size/depth limit. null renders as --.
GPU utilization and temperature use the GPU provider available on the PC
(for example `nvidia-smi`); unsupported hardware reports null.
The time is a display label, not a device clock or ordering key. Offline timing
uses device monotonic time, so PC clock changes do not extend validity.

Each `agents` entry is `{tool,state,id,text}`. `state` is one of `offline`,
`idle`, `running`, `ask`, `done`, or `error`; `id` is required for `ask` and
is limited to 36 printable ASCII characters, while `text` is limited to 48.
In the current landscape Agent page, the right-side Up/Down keys select DSH or
Codex. Confirm sends `XTE: agent_action approve TOOL REQUEST_ID`; the top Right
key sends `XTE: agent_action reject TOOL REQUEST_ID`; Back returns to the menu.
The PC driver accepts the action only
when the status bridge still reports the same tool, `ask` state, and request ID.
The bridge file is selected with `XTE_AGENT_STATUS` and defaults to
`%USERPROFILE%\\.xte-link\\agents.json`. When no bridge entry exists, the driver
reports whether the desktop process is running for sources without a live adapter.
DSH uses its live Remote adapter. Codex uses Windows Desktop IPC and takes
precedence over file data, including when disconnected. Its device ID is a fresh
32-character token mapped to the exact native request (with its original ID type),
conversation, turn, owner and connection lifetime. Only command and file-change
single-request accept decisions are supported. Rejection uses the advertised
decline option, or cancel when decline is absent (cancel ends the current turn).
Rule amendments and session approvals are never selected. Disconnection, owner departure, changed
requests and missing stream revisions invalidate old tokens. Desktop responses
acknowledge forwarding, not task completion; real stream state clears requests
and supplies task status. Failed or unconfirmed replies are not replayed.
Codex never uses keyboard emulation; a file-only Codex entry cannot approve an
actual Desktop request. A live DSH adapter failure never falls back to keyboard
emulation. Guarded foreground keyboard handling is retained only for the explicit
file bridge when no live DSH adapter exists. Credential-bearing DSH Remote
requests are restricted to loopback hosts and cannot follow redirects.

Firmware sends `XTE: accepted` on valid input, and `XTE: rendered ...` after a
screen update, including refresh duration and free heap. SDK diagnostic lines
share the return channel. Acceptance and rendering are separate; this version
has no sequence-correlated delivery acknowledgement.

Up and Down cycle through status, volume, Agent and settings pages. Confirm on
the settings page enters local menu navigation; Back returns to page navigation.
On the volume page, Left and Right send `XTE: volume_delta DELTA SEQUENCE`
to the PC driver. DELTA is -1/+1, -5/+5 or -10/+10 according to the saved volume
step (default 5). Other deltas are rejected. Older drivers accept only -5/+5;
the updated driver retains the same v2 framing and legacy sequence behavior.
Settings are stored locally and do not change the 15-second telemetry timeout
or the PC sampling interval. The device shows the local
prediction immediately with fast refresh. The driver applies the delta, reads
the actual endpoint volume, and immediately sends the previous metrics snapshot
with updated `volume` and `volume_ack`, without waiting for metric collection.
The timestamp remains that of the cached metrics. Until a matching confirmation
arrives, older frames cannot replace the prediction. After two seconds without
confirmation the device falls back to the last PC value. Legacy commands without
a sequence are also accepted by the driver. Demo mode does not change PC volume.

## LAN transport (existing v2 payloads)

TCP port 8765, discovered with mDNS `_xte-link._tcp.local.`; discovery advertises
the 12-character lowercase hex device ID and does not authenticate a peer.
USB enrollment stores a 32-byte PSK as 64 lowercase hex characters on the PC
and TF card. TF `/xte-link/network.json` also contains the WiFi password in
plaintext. PC configuration is `%USERPROFILE%\\.xte-link\\tray.json`.

The device sends an LF-terminated JSON challenge `{device,nonce}` with a fresh
32-character hex nonce. The client sends `{auth:HEX}` where HEX is
HMAC-SHA256(PSK, ASCII(`client:DEVICE:NONCE`)); the server replies with
`{ok:true,proof:HEX}` using ASCII(`server:DEVICE:NONCE`). Identity, nonce and
proof must validate before exchanging application messages.

Each authenticated packet is `sequence:u32be | length:u16be | payload | tag`.
Sequence starts at 1 in each direction and must increment exactly; start a new
session before overflow. Length is 1–385 bytes, including the required final LF.
The 32-byte tag is HMAC-SHA256(PSK, `ROLE + ":" + ASCII(NONCE) + header + payload`),
where ROLE is ASCII `C` from PC and `S` from device. Existing bounded ASCII JSON
and command lines remain unchanged inside this framing. Invalid size, sequence,
LF or tag closes the connection. Messages are authenticated, **not encrypted**.

An unauthenticated peer has a fixed approximately 5-second deadline; partial
bytes do not extend it. Authenticated sessions expire after approximately
15 seconds without a valid packet; PC heartbeats are sent every 5 seconds.
TF maintenance pauses device UI/network polling, so normal reconnection may
follow a long transfer. Pairing-key rotation and encryption are outside this
version's protocol.
