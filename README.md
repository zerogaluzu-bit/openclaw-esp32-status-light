# OpenClaw ESP32 Status Light

A small physical traffic light for one OpenClaw conversation.  An ESP32-C3
runs the LEDs; a read-only Python bridge on macOS watches one OpenClaw session
and sends a stable serial command such as `STATE:BUSY`.

This repository is deliberately split at a narrow boundary:

```text
OpenClaw lifecycle -> host/bridge.py -> USB serial -> ESP32-C3 -> three LEDs
```

The firmware and serial protocol are independent of OpenClaw versions.  The
only version-sensitive part is the lifecycle query in
`host/bridge.py::read_lifecycle()`.

## What each part does

- `firmware/boot.py` — MicroPython boot banner.
- `firmware/main.py` — LED effects and the line-based serial protocol.
- `host/bridge.py` — reads OpenClaw state, maps it to a light state, and keeps
  the board synchronized.
- `launchd/*.plist.example` — macOS background-service template.
- `host/tests/` — offline state-machine, SQLite, serial, and reconnect tests.
- `docs/ARCHITECTURE.md` — component contract and porting seam.
- `docs/TROUBLESHOOTING.md` — the few failure modes that mattered in practice.

## Tested reference setup

- Apple Silicon Mac mini (M4)
- macOS + launchd
- OpenClaw `2026.10.6`
- Python `3.14.3`; pyserial `3.5`
- ESP32-C3 native USB serial, Espressif VID/PID `303a:1001`
- MicroPython firmware with red/yellow/green LEDs on GPIO 3/4/5

Another OpenClaw version may use a different lifecycle source.  It should not
require a firmware change; see [Porting to another OpenClaw version](#porting-to-another-openclaw-version).

## Light language

- `IDLE` — slowly breathing green
- `BUSY` — alternating yellow and green
- `THINKING` — blinking yellow
- `WAIT_CONFIRM` — solid yellow
- `SUCCESS` — solid green for five seconds, then idle
- `ERROR` — blinking red
- `OFF` — solid red; the OpenClaw Gateway has remained unavailable

The light is ambient status, not an authorization control and not a health
guarantee.

## 1. Prepare the Mac

```bash
git clone https://github.com/zerogaluzu-bit/openclaw-esp32-status-light.git
cd openclaw-esp32-status-light

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Find the session key to monitor:

```bash
openclaw sessions --json --limit 20 \
  | jq -r '.sessions[] | [.key, .kind, .updatedAt] | @tsv'
```

Choose the direct conversation that owns the agent activity you want the light
to represent.  A session key identifies routing metadata; it is not an API
token.

## 2. Install or verify the ESP32 firmware

If the purchased unit already answers this repository's `STATE:<name>`
protocol, keep its working firmware.  Otherwise install MicroPython and copy:

```bash
python3 -m pip install mpremote
mpremote connect auto fs cp firmware/boot.py :boot.py
mpremote connect auto fs cp firmware/main.py :main.py
mpremote connect auto reset
```

The tested retail unit uses this physical mapping:

- GPIO 3 -> red LED
- GPIO 4 -> yellow LED
- GPIO 5 -> green LED
- GND -> common ground

Trust GPIO numbers rather than the wire colours on the unit: the sample unit's
wire colours did not match the LED colours.

## 3. Prove each layer before launchd

First check OpenClaw detection without touching the serial port:

```bash
.venv/bin/python host/bridge.py \
  --session-key 'REPLACE_WITH_SESSION_KEY' \
  --no-serial --once
```

Then test the board directly:

```bash
.venv/bin/python host/bridge.py --state IDLE
.venv/bin/python host/bridge.py --state BUSY
.venv/bin/python host/bridge.py --state WAIT_CONFIRM
.venv/bin/python host/bridge.py --state ERROR
```

Finally run the whole bridge in the foreground:

```bash
.venv/bin/python host/bridge.py \
  --session-key 'REPLACE_WITH_SESSION_KEY'
```

Press `Ctrl-C` after observing at least one real state transition.

## 4. Run it with launchd

Copy `launchd/com.openclaw.esp32-status-light.plist.example` to:

```text
~/Library/LaunchAgents/com.openclaw.esp32-status-light.plist
```

Edit four values in the copy:

1. absolute path to `.venv/bin/python`;
2. absolute path to `host/bridge.py`;
3. the chosen OpenClaw session key;
4. absolute paths for the two log files.

Then validate and load it:

```bash
plutil -lint ~/Library/LaunchAgents/com.openclaw.esp32-status-light.plist
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.openclaw.esp32-status-light.plist
launchctl print gui/$(id -u)/com.openclaw.esp32-status-light
```

To reload an edited plist:

```bash
launchctl bootout gui/$(id -u)/com.openclaw.esp32-status-light
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.openclaw.esp32-status-light.plist
```

## Configuration

Run `python host/bridge.py --help` for the complete list.  Common overrides:

- `--session-key` — required session to monitor.
- `--db` — OpenClaw state database; defaults to
  `~/.openclaw/state/openclaw.sqlite`.
- `--gateway-host`, `--gateway-port` — defaults to `127.0.0.1:18789`.
- `--port` — explicit `/dev/cu.*` path, or `auto`.
- `--usb-serial` — optional exact board serial when several identical ESP32s
  are attached.
- `--vid`, `--pid` — default to `0x303a`, `0x1001`.

Equivalent environment variables are documented by `--help` in the source:
`OPENCLAW_STATUS_SESSION_KEY`, `OPENCLAW_STATE_DB`,
`OPENCLAW_GATEWAY_HOST`, `OPENCLAW_GATEWAY_PORT`, `STATUS_LIGHT_PORT`,
`STATUS_LIGHT_USB_SERIAL`, `STATUS_LIGHT_USB_VID`, and
`STATUS_LIGHT_USB_PID`.

## Porting to another OpenClaw version

Do not rewrite the firmware first.  Keep the serial states stable and inspect
how that OpenClaw build exposes:

1. agent run started / finished / failed;
2. pending operator approval;
3. Gateway reachability;
4. the exact session identity to monitor.

If `audit_events` or `operator_approvals` changed, update only
`read_lifecycle()` and its tests.  If lifecycle moved away from SQLite, replace
that function with the current supported API and keep
`determine_lifecycle_state()` as the mapping layer.

The reference bridge opens SQLite read-only and never reads OpenClaw secrets.

## Tests

```bash
.venv/bin/python -m unittest discover -s host/tests -v
```

## Known boundary

The reference state source follows one selected session's `agent_run` events.
A detached background job owned by another session can therefore continue
while this light returns to idle.  That limitation is acceptable for an
ambient desk light; adapt the lifecycle source if the target OpenClaw version
exposes pending child/background work directly.

## License

MIT.  See [LICENSE](LICENSE).
