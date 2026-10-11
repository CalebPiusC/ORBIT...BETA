from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.server import run_app


class FakeHeartbeat:
    """Stands in for the real heartbeat so no test writes runtime/heartbeat.json."""

    class _Settings:
        interval_seconds = 60.0

    def __init__(self) -> None:
        self.settings = self._Settings()


class RecordingFlaskApp:
    def __init__(self) -> None:
        self.run_options: dict[str, object] | None = None
        self.config: dict[str, object] = {"HEARTBEAT": FakeHeartbeat()}

    def run(self, **kwargs: object) -> None:
        self.run_options = kwargs


class ServerLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every run_app test runs with the scheduler stubbed out: no real thread starts.
        patcher = patch("heartbeat.start_scheduler")
        self.scheduler = patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_app_starts_scheduler_for_the_attached_heartbeat(self) -> None:
        app = RecordingFlaskApp()
        heartbeat = app.config["HEARTBEAT"]
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            run_app(app)  # type: ignore[arg-type]

        self.scheduler.assert_called_once_with(heartbeat, interval_seconds=60.0)

    def test_run_app_builds_heartbeat_at_boot_when_none_attached(self) -> None:
        from config import HeartbeatSettings
        from heartbeat import Heartbeat

        app = RecordingFlaskApp()
        app.config["HEARTBEAT"] = None
        settings = HeartbeatSettings(plan_hour=10, checkin_gap_days=2, interval_seconds=60.0,
                                     ntfy_server="https://ntfy.sh", ntfy_topic="")
        with (
            patch("config.load_heartbeat_settings", return_value=settings),
            patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}),
        ):
            run_app(app)  # type: ignore[arg-type]
        built = app.config["HEARTBEAT"]
        self.assertIsInstance(built, Heartbeat)
        self.scheduler.assert_called_once_with(built, interval_seconds=60.0)

    def test_defaults_to_loopback_with_debug_disabled(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(
            app.run_options,
            {"host": "127.0.0.1", "port": 5000, "debug": False},
        )

    def test_preview_can_opt_into_external_bind_without_debugger(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "0.0.0.0", "PORT": "8123"}):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(
            app.run_options,
            {"host": "0.0.0.0", "port": 8123, "debug": False},
        )

    def test_invalid_port_fails_before_starting_server(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "0.0.0.0", "PORT": "0"}):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 65535"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)

    def test_demo_launcher_uses_shared_safe_runner(self) -> None:
        import run_demo

        fake_app = object()
        with (
            patch("run_demo.create_app", return_value=fake_app),
            patch("run_demo.run_app") as runner,
            patch("builtins.print"),
        ):
            run_demo.main()

        runner.assert_called_once_with(fake_app)


if __name__ == "__main__":
    unittest.main()
