# Architecture and replacement seams

## Stable boundary: serial protocol

The Mac sends one UTF-8 line:

```text
STATE:IDLE
STATE:BUSY
STATE:WAIT_CONFIRM
STATE:SUCCESS
STATE:ERROR
STATE:OFF
```

The board accepts a known state and replies with the exact acknowledgment:

```text
OK:BUSY
```

It also answers `PING` with `PONG`.  Unknown commands return `ERR:Unknown`.

This protocol is intentionally unaware of OpenClaw, databases, Telegram, or
models.  That is why another OpenClaw release should require a host-side
adapter change only.

## Firmware responsibilities

`firmware/main.py` owns:

- GPIO/PWM configuration;
- visual effects;
- command parsing and acknowledgment;
- continuing the last selected effect when the Mac is quiet.

It does not infer agent state and does not use Wi-Fi.

## Host responsibilities

`host/bridge.py` owns:

- choosing one serial device by explicit path, exact USB serial, or unique
  VID/PID;
- probing the local OpenClaw Gateway;
- reading one selected session's lifecycle and approval state;
- mapping that information to the serial state vocabulary;
- re-sending state periodically so USB unplug/replug recovers;
- suppressing brief red flashes during an ordinary Gateway reload;
- holding a process lock so launchd and a manual instance do not fight for the
  same serial port.

## OpenClaw-specific seam

Only these functions need knowledge of OpenClaw state semantics:

- `read_lifecycle(db_path, session_key)` — storage/API adapter;
- `determine_lifecycle_state(event, pending_approval)` — semantic mapping;
- `gateway_reachable(host, port)` — liveness probe.

The reference implementation was proven against OpenClaw `2026.10.6` and
expects:

- `audit_events` rows with `kind='agent_run'` and
  `agent.run.started` / `agent.run.finished` actions;
- `operator_approvals` rows with a source session, pending status, and expiry.

When porting, inspect the running version instead of force-creating old tables
or copying the whole database.  The database is opened with `mode=ro`.

## State priorities

1. a pending approval becomes `WAIT_CONFIRM`;
2. a started run becomes `BUSY`;
3. a recent successful finish becomes `SUCCESS` for five seconds;
4. failed or timed-out finish becomes `ERROR`;
5. cancellation and old success become `IDLE`;
6. sustained Gateway/storage unavailability becomes `OFF`.

Brief Gateway outages keep the last stable state for 30 seconds.  At startup,
an unavailable Gateway is shown as `OFF` immediately because no stable state
exists yet.

## Hardware identity

The tested ESP32-C3 exposes native USB serial as Espressif `303a:1001`.
Different boards can override VID/PID or use an explicit `/dev/cu.*` path.
If several identical boards are attached, set the exact USB serial number.
