#!/usr/bin/env python3
"""Bridge OpenClaw run state to an ESP32-C3 three-colour status light.

The OpenClaw-specific seam is intentionally small: read_lifecycle() reads the
current audit schema, and determine_lifecycle_state() maps it to the stable
serial protocol.  If another OpenClaw version stores lifecycle state
differently, adapt those functions and leave the firmware/protocol unchanged.
"""

import argparse
from contextlib import closing
import fcntl
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import time

import serial
from serial.tools import list_ports


DEFAULT_BAUD = 115200
DEFAULT_POLL_INTERVAL = 0.5
SERIAL_KEEPALIVE_SECONDS = 2.0
GATEWAY_OFF_GRACE_SECONDS = 30.0
SQLITE_TIMEOUT_SECONDS = 2.0
SQLITE_READ_ATTEMPTS = 3
SQLITE_RETRY_BACKOFF_SECONDS = 0.3
DONE_HOLD_MS = 5_000
DEFAULT_DB = Path.home() / ".openclaw/state/openclaw.sqlite"
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 18789
DEFAULT_ESP32_VID = 0x303A
DEFAULT_ESP32_PID = 0x1001
DEFAULT_PID_FILE = Path.home() / "Library/Caches/openclaw-status-light.pid"

SERIAL_STATES = (
    "IDLE",
    "THINKING",
    "BUSY",
    "SUCCESS",
    "ERROR",
    "WAIT_CONFIRM",
    "OFF",
)

serial_handle = None
shutdown = False
pid_handle = None


def log(message):
    print(message, flush=True)


def parse_hex_or_decimal(value):
    return int(str(value), 0)


def resolve_serial_port(
    requested="auto",
    usb_serial=None,
    vid=DEFAULT_ESP32_VID,
    pid=DEFAULT_ESP32_PID,
    ports=None,
):
    """Find the board by explicit path, USB serial, or unique VID/PID."""
    if requested and requested != "auto":
        return requested
    candidates = list(ports if ports is not None else list_ports.comports())
    if usb_serial:
        exact = [p.device for p in candidates if p.serial_number == usb_serial]
        if exact:
            return exact[0]
    matching_model = [
        p.device for p in candidates if p.vid == vid and p.pid == pid
    ]
    return matching_model[0] if len(matching_model) == 1 else None


def acquire_instance_lock(path=DEFAULT_PID_FILE):
    """Hold an atomic process lock and expose its owner PID for diagnostics."""
    global pid_handle
    path.parent.mkdir(parents=True, exist_ok=True)
    pid_handle = path.open("a+")
    try:
        fcntl.flock(pid_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid_handle.seek(0)
        owner = pid_handle.read().strip() or "unknown"
        raise RuntimeError(f"bridge already running (PID {owner})")
    pid_handle.seek(0)
    pid_handle.truncate()
    pid_handle.write(str(os.getpid()))
    pid_handle.flush()


def release_instance_lock(path=DEFAULT_PID_FILE):
    global pid_handle
    if pid_handle is None:
        return
    try:
        fcntl.flock(pid_handle.fileno(), fcntl.LOCK_UN)
        pid_handle.close()
        path.unlink(missing_ok=True)
    finally:
        pid_handle = None


def open_serial(args):
    global serial_handle
    port = resolve_serial_port(
        args.port, args.usb_serial, args.vid, args.pid
    )
    if not port:
        log("  WARNING: ESP32 not found by USB identity")
        return False
    try:
        serial_handle = serial.Serial(
            port,
            args.baud,
            timeout=2,
            write_timeout=2,
            exclusive=True,
        )
        log(f"  ESP32 connected: {port}")
        # Opening a native USB serial port can reset the ESP32-C3.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            line = serial_handle.readline().decode(errors="replace").strip()
            if line == "READY":
                return True
            if line:
                log(f"  [ESP32] {line}")
        serial_handle.reset_input_buffer()
        return True
    except (serial.SerialException, OSError) as exc:
        serial_handle = None
        log(f"  WARNING: serial unavailable: {exc}")
        return False


def close_serial():
    global serial_handle
    if serial_handle is not None:
        try:
            serial_handle.close()
        finally:
            serial_handle = None


def send_state(state, args):
    """Send one state and require the firmware's exact acknowledgment."""
    global serial_handle
    if serial_handle is None and not open_serial(args):
        return False
    try:
        serial_handle.write(f"STATE:{state}\n".encode())
        # Do not call Serial.flush() here.  On macOS it maps to tcdrain(),
        # which can block indefinitely after some ESP32 resets.
        response = serial_handle.readline().decode(errors="replace").strip()
        if response == f"OK:{state}":
            return True
        log(f"  WARNING: unexpected ESP32 response: {response or '<empty>'}")
        return False
    except (serial.SerialException, OSError) as exc:
        log(f"  WARNING: serial write failed: {exc}")
        close_serial()
        return False


def gateway_reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def read_lifecycle(db_path, session_key):
    """Read the latest agent run and pending approval from OpenClaw SQLite.

    This is the only storage-schema-specific function in the bridge.
    """
    uri = f"file:{Path(db_path)}?mode=ro"
    last_error = None
    for attempt in range(SQLITE_READ_ATTEMPTS):
        try:
            # sqlite3.Connection.__exit__ does not close the connection.
            with closing(sqlite3.connect(
                uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS
            )) as db:
                db.row_factory = sqlite3.Row
                event = db.execute(
                    """SELECT sequence, occurred_at, action, status,
                              error_code, run_id
                       FROM audit_events
                       WHERE session_key = ? AND kind = 'agent_run'
                       ORDER BY sequence DESC LIMIT 1""",
                    (session_key,),
                ).fetchone()
                pending = db.execute(
                    """SELECT 1 FROM operator_approvals
                       WHERE source_session_key = ? AND status = 'pending'
                         AND expires_at_ms > ? LIMIT 1""",
                    (session_key, int(time.time() * 1000)),
                ).fetchone()
            return (dict(event) if event else None), bool(pending)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            last_error = exc
            if attempt + 1 < SQLITE_READ_ATTEMPTS:
                time.sleep(SQLITE_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error


def determine_lifecycle_state(event, pending_approval=False, now_ms=None):
    if pending_approval:
        return "WAIT_CONFIRM"
    if not event:
        return "IDLE"
    action = event.get("action")
    status = event.get("status")
    if action == "agent.run.started" and status == "started":
        return "BUSY"
    if action != "agent.run.finished":
        return "IDLE"
    if status == "succeeded":
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        occurred_at = event.get("occurred_at") or 0
        return "SUCCESS" if now_ms - occurred_at < DONE_HOLD_MS else "IDLE"
    if status in {"failed", "timed_out"}:
        return "ERROR"
    return "IDLE"


def detect_state(args):
    if not gateway_reachable(args.gateway_host, args.gateway_port):
        log("  WARNING: Gateway probe unavailable; OFF grace active")
        return "OFF"
    try:
        event, pending = read_lifecycle(args.db, args.session_key)
        return determine_lifecycle_state(event, pending)
    except (OSError, sqlite3.Error) as exc:
        log(f"  WARNING: lifecycle database unavailable: {exc}")
        return "OFF"


def should_deliver(state, delivered_state, last_delivery_at, now=None):
    now = time.monotonic() if now is None else now
    return (
        state != delivered_state
        or now - last_delivery_at >= SERIAL_KEEPALIVE_SECONDS
    )


def apply_gateway_off_grace(detected_state, stable_state, off_since, now=None):
    now = time.monotonic() if now is None else now
    if detected_state != "OFF":
        return detected_state, None
    if stable_state is None or stable_state == "OFF":
        return "OFF", off_since or now
    off_since = now if off_since is None else off_since
    if now - off_since < GATEWAY_OFF_GRACE_SECONDS:
        return stable_state, off_since
    return "OFF", off_since


def on_signal(_signal, _frame):
    global shutdown
    shutdown = True


def build_parser():
    parser = argparse.ArgumentParser(description="OpenClaw ESP32 status-light bridge")
    parser.add_argument(
        "--session-key",
        default=os.environ.get("OPENCLAW_STATUS_SESSION_KEY", ""),
        help="OpenClaw session key to monitor",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("OPENCLAW_STATE_DB", DEFAULT_DB)),
        help="OpenClaw SQLite state database",
    )
    parser.add_argument(
        "--gateway-host",
        default=os.environ.get("OPENCLAW_GATEWAY_HOST", DEFAULT_GATEWAY_HOST),
    )
    parser.add_argument(
        "--gateway-port",
        type=int,
        default=int(os.environ.get("OPENCLAW_GATEWAY_PORT", DEFAULT_GATEWAY_PORT)),
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("STATUS_LIGHT_PORT", "auto"),
        help="serial path, or auto",
    )
    parser.add_argument(
        "--usb-serial",
        default=os.environ.get("STATUS_LIGHT_USB_SERIAL") or None,
        help="optional exact USB serial number",
    )
    parser.add_argument(
        "--vid",
        type=parse_hex_or_decimal,
        default=parse_hex_or_decimal(os.environ.get("STATUS_LIGHT_USB_VID", "0x303a")),
    )
    parser.add_argument(
        "--pid",
        type=parse_hex_or_decimal,
        default=parse_hex_or_decimal(os.environ.get("STATUS_LIGHT_USB_PID", "0x1001")),
    )
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--state", choices=SERIAL_STATES)
    parser.add_argument(
        "--no-serial", action="store_true", help="detect and print state only"
    )
    return parser


def main(argv=None):
    global shutdown
    shutdown = False
    args = build_parser().parse_args(argv)
    if not args.state and not args.session_key:
        raise SystemExit(
            "--session-key or OPENCLAW_STATUS_SESSION_KEY is required "
            "unless --state is used"
        )

    try:
        acquire_instance_lock()
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 2

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    log(f"Status-light bridge | {args.port} | poll {args.interval:g}s")
    last_state = None
    delivered_state = None
    last_delivery_at = 0.0
    gateway_off_since = None
    try:
        while not shutdown:
            detected = args.state or detect_state(args)
            state, gateway_off_since = apply_gateway_off_grace(
                detected, last_state, gateway_off_since
            )
            if state != last_state:
                log(f"  [{time.strftime('%H:%M:%S')}] {last_state or 'NONE'} -> {state}")
                last_state = state

            if args.no_serial:
                delivered_state = state
            elif should_deliver(state, delivered_state, last_delivery_at):
                if send_state(state, args):
                    delivered_state = state
                    last_delivery_at = time.monotonic()
                else:
                    delivered_state = None

            if args.once or args.state:
                return 0 if (args.no_serial or delivered_state == state) else 1
            time.sleep(max(0.25, args.interval))
    finally:
        # Keep the last light state during a normal bridge restart.
        close_serial()
        release_instance_lock()
        log("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
