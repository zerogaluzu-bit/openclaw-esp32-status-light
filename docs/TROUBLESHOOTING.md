# Troubleshooting

## Board is not found

List what pyserial sees:

```bash
.venv/bin/python -m serial.tools.list_ports -v
```

Use `--port /dev/cu.usbmodem...` for a direct test.  If several boards share
the same VID/PID, pass `--usb-serial` so discovery is unambiguous.

## Manual test says the port is busy

The launchd service may already own it.  Inspect the service and its process:

```bash
launchctl print gui/$(id -u)/com.openclaw.esp32-status-light
pgrep -fal 'status-light.*bridge.py'
```

Stop the service before a foreground hardware test, then start it again after.

## Solid red

`OFF` means the configured Gateway endpoint or lifecycle source is unavailable.
Check:

```bash
openclaw status
ls -l ~/.openclaw/state/openclaw.sqlite
```

If OpenClaw itself is healthy, confirm the target version still provides the
tables/fields used by `read_lifecycle()`.

## The light never reacts to conversation activity

First run detection without serial:

```bash
.venv/bin/python host/bridge.py \
  --session-key 'YOUR_SESSION_KEY' --no-serial --once
```

If it always reports `IDLE`, the usual causes are the wrong session key or a
different lifecycle schema in that OpenClaw release.

## USB reconnect leaves stale state

The bridge re-sends the current state every two seconds.  If reconnect still
fails, inspect the logs for the selected device and acknowledgment.  Avoid
adding `Serial.flush()` as a fix: on macOS it maps to `tcdrain()` and was
observed blocking indefinitely after an ESP32 reset.

## Status turns red only after hours

Keep the explicit `contextlib.closing(sqlite3.connect(...))`.  Python's SQLite
connection context manager commits or rolls back but does not close the
connection; a polling loop can otherwise leak DB/WAL file descriptors until
even the Gateway socket probe fails.

## Brief red flash during OpenClaw restart

The bridge has a 30-second OFF grace period after it already knows a stable
state.  Increase it only if normal restarts on the target machine consistently
take longer; do not hide a genuinely unavailable Gateway indefinitely.
