from __future__ import annotations

import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.server import DEFAULT_SECRET_KEY, run_app
from app.voice import VoiceDependencyReport


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
        # A real Flask app always carries a session secret; run_app refuses to
        # serve on the committed default, so the stand-in has a real one too.
        self.secret_key = "test-session-secret-not-the-committed-default"

    def run(self, **kwargs: object) -> None:
        self.run_options = kwargs


class ServerLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every run_app test runs with the scheduler stubbed out: no real thread starts.
        patcher = patch("heartbeat.start_scheduler")
        self.scheduler = patcher.start()
        self.addCleanup(patcher.stop)
        self._preserve_orbit_logger()

    def _preserve_orbit_logger(self) -> None:
        """Hand the "orbit" logger back after each test: run_app configures it once."""
        orbit = logging.getLogger("orbit")
        handlers, level, propagate = list(orbit.handlers), orbit.level, orbit.propagate

        def restore() -> None:
            orbit.handlers[:] = handlers
            orbit.setLevel(level)
            orbit.propagate = propagate

        self.addCleanup(restore)

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

    def test_run_app_reports_what_the_voice_dependencies_are_at_boot(self) -> None:
        class FakeVoiceService:
            def dependencies(self):
                return VoiceDependencyReport(
                    model="base",
                    device="cpu",
                    compute_type="int8",
                    pyav="18.1.0",
                    ffmpeg_libraries={"libavcodec": "62.28.102"},
                    ffmpeg_program=None,
                    model_cached=False,
                    cache_dir="/tmp/hf-cache",
                )

        app = RecordingFlaskApp()
        app.config["VOICE_SERVICE"] = FakeVoiceService()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            with self.assertLogs("orbit.server", level="INFO") as captured:
                run_app(app)  # type: ignore[arg-type]

        [record] = [r for r in captured.records if "voice dependencies" in r.getMessage()]
        logged = record.getMessage()
        self.assertIn("pyav=18.1.0", logged)
        self.assertIn("ffmpeg_program=not installed", logged)
        self.assertIn("ffmpeg_program_required=False", logged)
        self.assertIn("model_cached=False", logged)

    def test_run_app_survives_a_voice_service_that_cannot_report(self) -> None:
        class OpaqueVoiceService:
            def dependencies(self):
                raise RuntimeError("no diagnostics here")

        app = RecordingFlaskApp()
        app.config["VOICE_SERVICE"] = OpaqueVoiceService()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            with self.assertLogs("orbit.server", level="ERROR") as captured:
                run_app(app)  # type: ignore[arg-type]

        self.assertTrue(
            any("voice dependency diagnostics" in r.getMessage() for r in captured.records)
        )

    def test_run_app_refuses_to_start_without_a_session_secret(self) -> None:
        """No secret means forged sessions, so the server must not come up."""
        app = RecordingFlaskApp()
        app.secret_key = None
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "ORBIT_SECRET_KEY is not set"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)
        self.scheduler.assert_not_called()

    def test_run_app_refuses_the_committed_default_secret(self) -> None:
        """The fallback is public in this repo; serving on it is forgeable."""
        app = RecordingFlaskApp()
        app.secret_key = DEFAULT_SECRET_KEY
        with patch.dict(
            os.environ,
            {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": DEFAULT_SECRET_KEY},
        ):
            with self.assertRaisesRegex(RuntimeError, "committed default"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)
        self.scheduler.assert_not_called()

    def test_run_app_accepts_a_real_secret_from_the_environment(self) -> None:
        app = RecordingFlaskApp()
        app.secret_key = None
        with patch.dict(
            os.environ,
            {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": "a-real-random-secret"},
        ):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(app.run_options, {"host": "127.0.0.1", "port": 5000, "debug": False})

    def test_demo_launcher_uses_shared_safe_runner(self) -> None:
        import run_demo

        # SimpleNamespace, not object(): the launcher sets a per-run session
        # secret on the app, which a bare object() cannot carry.
        fake_app = SimpleNamespace()
        with (
            patch("run_demo.create_app", return_value=fake_app),
            patch("run_demo.run_app") as runner,
            patch("builtins.print"),
        ):
            run_demo.main()

        runner.assert_called_once_with(fake_app)

    def test_demo_launcher_mints_a_secret_instead_of_using_the_default(self) -> None:
        """The stub launcher must not fall back to the committed key."""
        import run_demo

        fake_app = SimpleNamespace()
        with (
            patch("run_demo.create_app", return_value=fake_app),
            patch("run_demo.run_app"),
            patch("builtins.print"),
            patch.dict(os.environ, {"ORBIT_SECRET_KEY": ""}),
        ):
            run_demo.main()

        self.assertNotEqual(fake_app.secret_key, DEFAULT_SECRET_KEY)
        self.assertTrue(fake_app.secret_key)


if __name__ == "__main__":
    unittest.main()
