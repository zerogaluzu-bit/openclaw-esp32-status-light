import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).parents[1] / "bridge.py"
SPEC = importlib.util.spec_from_file_location("status_light_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def event(action, status, occurred_at=10_000):
    return {"action": action, "status": status, "occurred_at": occurred_at}


def args(**overrides):
    values = {
        "port": "auto",
        "usb_serial": None,
        "vid": bridge.DEFAULT_ESP32_VID,
        "pid": bridge.DEFAULT_ESP32_PID,
        "baud": bridge.DEFAULT_BAUD,
        "gateway_host": "127.0.0.1",
        "gateway_port": 18789,
        "db": Path("state.sqlite"),
        "session_key": "agent:main:test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LifecycleStateTests(unittest.TestCase):
    def test_no_history_is_idle(self):
        self.assertEqual(bridge.determine_lifecycle_state(None), "IDLE")

    def test_started_is_busy(self):
        self.assertEqual(
            bridge.determine_lifecycle_state(event("agent.run.started", "started")),
            "BUSY",
        )

    def test_pending_approval_overrides_busy(self):
        self.assertEqual(
            bridge.determine_lifecycle_state(
                event("agent.run.started", "started"), pending_approval=True
            ),
            "WAIT_CONFIRM",
        )

    def test_recent_success_is_success(self):
        self.assertEqual(
            bridge.determine_lifecycle_state(
                event("agent.run.finished", "succeeded"), now_ms=14_999
            ),
            "SUCCESS",
        )

    def test_old_success_is_idle(self):
        self.assertEqual(
            bridge.determine_lifecycle_state(
                event("agent.run.finished", "succeeded"), now_ms=15_000
            ),
            "IDLE",
        )

    def test_failure_and_timeout_are_errors(self):
        for status in ("failed", "timed_out"):
            with self.subTest(status=status):
                self.assertEqual(
                    bridge.determine_lifecycle_state(
                        event("agent.run.finished", status)
                    ),
                    "ERROR",
                )

    def test_cancelled_is_not_error(self):
        self.assertEqual(
            bridge.determine_lifecycle_state(
                event("agent.run.finished", "cancelled")
            ),
            "IDLE",
        )


class SerialDiscoveryTests(unittest.TestCase):
    def test_explicit_port_wins(self):
        self.assertEqual(
            bridge.resolve_serial_port("/dev/test", ports=[]), "/dev/test"
        )

    def test_exact_usb_serial_wins(self):
        ports = [SimpleNamespace(
            device="/dev/cu.usbmodem999",
            serial_number="BOARD-A",
            vid=bridge.DEFAULT_ESP32_VID,
            pid=bridge.DEFAULT_ESP32_PID,
        )]
        self.assertEqual(
            bridge.resolve_serial_port(
                "auto", usb_serial="BOARD-A", ports=ports
            ),
            "/dev/cu.usbmodem999",
        )

    def test_model_fallback_requires_unique_board(self):
        def board(device):
            return SimpleNamespace(
                device=device,
                serial_number=None,
                vid=bridge.DEFAULT_ESP32_VID,
                pid=bridge.DEFAULT_ESP32_PID,
            )

        self.assertEqual(
            bridge.resolve_serial_port("auto", ports=[board("one")]), "one"
        )
        self.assertIsNone(
            bridge.resolve_serial_port(
                "auto", ports=[board("one"), board("two")]
            )
        )

    def test_send_state_does_not_call_blocking_flush(self):
        fake_serial = MagicMock()
        fake_serial.readline.return_value = b"OK:IDLE\n"
        fake_serial.flush.side_effect = AssertionError("flush must not be called")

        with patch.object(bridge, "serial_handle", fake_serial):
            self.assertTrue(bridge.send_state("IDLE", args()))

        fake_serial.write.assert_called_once_with(b"STATE:IDLE\n")
        fake_serial.flush.assert_not_called()

    def test_missing_ack_keeps_serial_open_for_retry(self):
        fake_serial = MagicMock()
        fake_serial.readline.return_value = b""

        with patch.object(bridge, "serial_handle", fake_serial), patch.object(
            bridge, "close_serial"
        ) as close_serial:
            self.assertFalse(bridge.send_state("IDLE", args()))

        close_serial.assert_not_called()

    def test_keepalive_resends_unchanged_state(self):
        deadline = 10.0 + bridge.SERIAL_KEEPALIVE_SECONDS
        self.assertFalse(
            bridge.should_deliver("IDLE", "IDLE", 10.0, now=deadline - 0.1)
        )
        self.assertTrue(
            bridge.should_deliver("IDLE", "IDLE", 10.0, now=deadline)
        )
        self.assertTrue(
            bridge.should_deliver("BUSY", "IDLE", deadline - 0.1, now=deadline)
        )


class GracePeriodTests(unittest.TestCase):
    def test_brief_gateway_reload_keeps_last_stable_state(self):
        state, off_since = bridge.apply_gateway_off_grace(
            "OFF", "BUSY", None, now=100.0
        )
        self.assertEqual(state, "BUSY")
        state, off_since = bridge.apply_gateway_off_grace(
            "OFF", state, off_since, now=109.9
        )
        self.assertEqual(state, "BUSY")
        state, off_since = bridge.apply_gateway_off_grace(
            "BUSY", state, off_since, now=110.0
        )
        self.assertEqual(state, "BUSY")
        self.assertIsNone(off_since)

    def test_sustained_gateway_outage_becomes_off(self):
        state, off_since = bridge.apply_gateway_off_grace(
            "OFF", "IDLE", None, now=100.0
        )
        state, _ = bridge.apply_gateway_off_grace(
            "OFF",
            state,
            off_since,
            now=100.0 + bridge.GATEWAY_OFF_GRACE_SECONDS,
        )
        self.assertEqual(state, "OFF")

    def test_startup_outage_is_immediately_off(self):
        state, _ = bridge.apply_gateway_off_grace(
            "OFF", None, None, now=100.0
        )
        self.assertEqual(state, "OFF")


class DatabaseTests(unittest.TestCase):
    def test_detect_state_uses_lifecycle_when_gateway_is_up(self):
        active = event("agent.run.started", "started")
        with patch.object(bridge, "gateway_reachable", return_value=True), patch.object(
            bridge, "read_lifecycle", return_value=(active, False)
        ):
            self.assertEqual(bridge.detect_state(args()), "BUSY")

    def test_detect_state_is_off_when_gateway_is_down(self):
        with patch.object(bridge, "gateway_reachable", return_value=False):
            self.assertEqual(bridge.detect_state(args()), "OFF")

    def test_read_lifecycle_retries_transient_sqlite_error(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        with patch.object(
            bridge.sqlite3,
            "connect",
            side_effect=[sqlite3.OperationalError("locked"), connection],
        ) as connect, patch.object(bridge.time, "sleep") as sleep:
            self.assertEqual(
                bridge.read_lifecycle("state.sqlite", "agent:main:test"),
                (None, False),
            )
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(bridge.SQLITE_RETRY_BACKOFF_SECONDS)
        connection.close.assert_called_once_with()

    def test_reads_latest_event_and_pending_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "state.sqlite"
            with sqlite3.connect(db_path) as db:
                db.executescript("""
                    CREATE TABLE audit_events (
                      sequence INTEGER, occurred_at INTEGER, action TEXT,
                      status TEXT, error_code TEXT, run_id TEXT,
                      session_key TEXT, kind TEXT
                    );
                    CREATE TABLE operator_approvals (
                      source_session_key TEXT, status TEXT, expires_at_ms INTEGER
                    );
                """)
                db.execute(
                    "INSERT INTO audit_events VALUES (1,1,'agent.run.finished',"
                    "'succeeded',NULL,'old',?,'agent_run')",
                    ("agent:main:test",),
                )
                db.execute(
                    "INSERT INTO audit_events VALUES (2,2,'agent.run.started',"
                    "'started',NULL,'new',?,'agent_run')",
                    ("agent:main:test",),
                )
                db.execute(
                    "INSERT INTO operator_approvals VALUES (?, 'pending', ?)",
                    ("agent:main:test", 9_999_999_999_999),
                )
            latest, pending = bridge.read_lifecycle(
                db_path, "agent:main:test"
            )
            self.assertEqual(latest["run_id"], "new")
            self.assertTrue(pending)


class CliTests(unittest.TestCase):
    def test_hex_or_decimal(self):
        self.assertEqual(bridge.parse_hex_or_decimal("0x303a"), 0x303A)
        self.assertEqual(bridge.parse_hex_or_decimal("12346"), 12346)

    def test_default_session_key_is_not_person_specific(self):
        with patch.dict(os.environ, {"OPENCLAW_STATUS_SESSION_KEY": ""}):
            parsed = bridge.build_parser().parse_args([])
        self.assertEqual(parsed.session_key, "")


if __name__ == "__main__":
    unittest.main()
