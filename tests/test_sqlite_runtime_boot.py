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


def test_the_legacy_heartbeat_import_does_not_duplicate_a_todays_notice(tmp_path):
    """The import is the source of the duplicate-notice bug; pin the whole path.

    PR #20 copied ``runtime/heartbeat.json`` into the account row. When the file
    held a notice for today but no matching ``fired`` marker, the first tick
    after import opened a second, identical notice — the two cards the home
    screen showed at once. The import now rebuilds the marker from the notice.
    """
    import json
    from datetime import datetime, time, timezone

    from app.store import DEFAULT_USER_ID
    from heartbeat.schedule import local_time

    now = datetime.now(timezone.utc)
    legacy_path = tmp_path / "heartbeat.json"
    notice = {
        "id": "hb-legacy000001",
        "kind": "plan_missing",
        "text": (
            "It is past 10:00 and there is no plan saved for today yet. "
            "Tell Orbit your plan in chat to save it."
        ),
        "created_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dismissed_at": None,
        "push": "off",
        "manual": False,
    }
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "paused": False,
                "last_checkin_at": now.astimezone(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "fired": {},
                "notices": [notice],
            }
        ),
        encoding="utf-8",
    )

    path = tmp_path / "orbit.sqlite3"
    app = create_app()
    app.secret_key = "runtime-test-secret"
    env = {
        "ORBIT_SECRET_KEY": "runtime-test-secret",
        "ORBIT_DB_PATH": str(path),
        "ORBIT_HEARTBEAT_PATH": str(legacy_path),
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

    heartbeat = app.config["HEARTBEAT"].for_account(DEFAULT_USER_ID)
    assert [n["id"] for n in heartbeat.snapshot()["notices"]] == [notice["id"]]

    # A tick on the same local day, after the plan hour and out of quiet hours,
    # must not raise a second copy of the same notice.
    local_today = local_time(now).date()
    moment = datetime.combine(local_today, time(11, 30), tzinfo=timezone.utc).astimezone(local_time(now).tzinfo)
    assert heartbeat.tick(moment) == []
    assert [n["id"] for n in heartbeat.snapshot(moment)["notices"]] == [notice["id"]]
