from pathlib import Path
from unittest.mock import patch

from app.server import create_app, run_app
from app.sqlite_accounts import SQLiteAccountStore
from app.sqlite_projects import SQLiteProjectStore
from app.sqlite_store import SQLiteStore
from heartbeat.sqlite_store import SQLiteHeartbeatManager
from memory.sqlitefacts import SQLiteFactStore


def test_normal_runtime_boot_uses_sqlite_for_persistent_data(tmp_path):
    path = tmp_path / "new" / "nested" / "orbit.sqlite3"
    app = create_app()
    app.secret_key = "runtime-test-secret"
    env = {
        "ORBIT_SECRET_KEY": "runtime-test-secret",
        "ORBIT_DB_PATH": str(path),
        "ORBIT_PROJECTS_PATH": str(tmp_path / "legacy-projects.json"),
        "ORBIT_ACCOUNTS_PATH": str(tmp_path / "legacy-accounts.json"),
        "ORBIT_FACTS_PATH": str(tmp_path / "legacy-facts.txt"),
        "ORBIT_NOTES_PATH": str(tmp_path / "legacy-notes.txt"),
        "ORBIT_PLANS_PATH": str(tmp_path / "legacy-plans.json"),
    }
    with (
        patch.dict("os.environ", env),
        patch("heartbeat.start_scheduler"),
        patch.object(app, "run"),
    ):
        run_app(app)

    assert path.is_file()
    assert isinstance(app.config["STORE"], SQLiteStore)
    assert isinstance(app.config["PROJECTS"], SQLiteProjectStore)
    assert isinstance(app.config["FACTS"], SQLiteFactStore)
    assert isinstance(app.config["ACCOUNTS"], SQLiteAccountStore)
    assert isinstance(app.config["HEARTBEAT"], SQLiteHeartbeatManager)
    assert app.config["ACTIONS"]._database.path == path.resolve()
