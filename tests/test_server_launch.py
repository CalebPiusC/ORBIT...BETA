from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import app.server
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


class DotenvStartupTests(unittest.TestCase):
    """The project-root .env must be loaded before the app reads the environment.

    ``python -m app.server`` used to refuse to start when
    ORBIT_SECRET_KEY lived only in .env: create_app() read the
    variable before anything had loaded the file, so the app baked
    in the committed default secret and run_app() rightly refused.
    """

    SECRET = "dotenv-only-secret-" + "s" * 45

    def setUp(self) -> None:
        self._original_secret = os.environ.get("ORBIT_SECRET_KEY")
        os.environ.pop("ORBIT_SECRET_KEY", None)
        self.addCleanup(self._restore_secret)
        # Leave app.server bound to the real project .env, whatever
        # the test reloaded it against.
        self.addCleanup(importlib.reload, app.server)

    def _restore_secret(self) -> None:
        if self._original_secret is None:
            os.environ.pop("ORBIT_SECRET_KEY", None)
        else:
            os.environ["ORBIT_SECRET_KEY"] = self._original_secret

    def _start_against(self, env_file: Path) -> Any:
        """Re-import app.server so its startup .env load reads ``env_file``."""
        with patch("config.ENV_FILE", env_file):
            importlib.reload(app.server)
            return app.server.create_app()

    def test_app_starts_with_the_secret_key_only_in_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f"ORBIT_SECRET_KEY={self.SECRET}\n")

            web_app = self._start_against(env_file)

        self.assertEqual(web_app.secret_key, self.SECRET)
        app.server._require_real_secret_key(web_app)  # must not raise

    def test_app_still_refuses_when_the_key_is_missing_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-dotenv"

            web_app = self._start_against(missing)

        self.assertEqual(web_app.secret_key, DEFAULT_SECRET_KEY)
        with self.assertRaisesRegex(RuntimeError, "ORBIT_SECRET_KEY is not set"):
            app.server._require_real_secret_key(web_app)

    def test_launch_from_another_directory_loads_the_project_dotenv(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with (
            tempfile.TemporaryDirectory() as launch_dir,
            tempfile.TemporaryDirectory() as env_dir,
        ):
            env_file = Path(env_dir) / ".env"
            env_file.write_text(f"ORBIT_SECRET_KEY={self.SECRET}\n")
            launcher = (
                "import sys\n"
                "from pathlib import Path\n"
                "import config\n"
                "config.ENV_FILE = Path(sys.argv[1])\n"
                "import app.server as server\n"
                "app = server.create_app()\n"
                "server._require_real_secret_key(app)\n"
                "assert app.secret_key == sys.argv[2]\n"
                "print('LAUNCHED')\n"
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key != "ORBIT_SECRET_KEY"
            }
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(repo_root), environment.get("PYTHONPATH", "")]
            )

            result = subprocess.run(
                [sys.executable, "-c", launcher, str(env_file), self.SECRET],
                cwd=launch_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LAUNCHED", result.stdout)


if __name__ == "__main__":
    unittest.main()
