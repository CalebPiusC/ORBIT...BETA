from datetime import date, datetime, timezone

from app.analytics import build_analytics
from app.database import Database
from config import HeartbeatSettings
from heartbeat.service import KIND_PLAN, _empty_state, merge_imported_state
from heartbeat.sqlite_store import SQLiteHeartbeatManager
from tools.registry import AuditLog
from zoneinfo import ZoneInfo

LAGOS = ZoneInfo("Africa/Lagos")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=LAGOS)


def settings():
    return HeartbeatSettings(
        plan_hour=10,
        checkin_gap_days=3,
        interval_seconds=60,
        ntfy_server="https://ntfy.sh",
        ntfy_topic="",
    )


def test_heartbeat_state_and_events_are_isolated_per_account(tmp_path):
    database = Database(tmp_path / "accounts.sqlite3")
    manager = SQLiteHeartbeatManager(settings=settings(), database=database)
    account_a = manager.for_account("account-A")
    account_b = manager.for_account("account-B")

    account_a.set_paused(True, source="test")
    today = date.today()
    with manager.account_context("account-A"):
        manager.add("Account A plan", today, "2026-01-01T00:00:00Z")
        assert manager.has_plan(today)
    with manager.account_context("account-B"):
        assert not manager.has_plan(today)
    assert account_a.snapshot()["paused"] is True
    assert account_b.snapshot()["paused"] is False

    # The persisted state is restored by a fresh manager after restart.
    restarted = SQLiteHeartbeatManager(settings=settings(), database=Database(database.path))
    assert restarted.for_account("account-A").snapshot()["paused"] is True
    assert restarted.for_account("account-B").snapshot()["paused"] is False
    assert not restarted.for_account("account-A").dismiss("B-only-notice", source="test")

    with database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM heartbeat_reminders WHERE account_id = ?", ("account-A",)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM heartbeat_reminders WHERE account_id = ?", ("account-B",)
        ).fetchone()[0] == 0


def test_analytics_reads_only_the_requested_accounts_rows(tmp_path):
    database = Database(tmp_path / "analytics.sqlite3")
    audit_a = AuditLog(database=database, account_id="account-A")
    audit_b = AuditLog(database=database, account_id="account-B")
    audit_a.record_event("chat_message_received", surface="home")
    audit_a.record_event("message_rated", rating="up")
    audit_b.record_event("chat_message_received", surface="home")
    now = datetime.now(timezone.utc)

    report_a = build_analytics(database, account_id="account-A", now=now)
    report_b = build_analytics(database, account_id="account-B", now=now)
    assert report_a.messages_total == 1
    assert report_a.ratings_up == 1
    assert report_b.messages_total == 1
    assert report_b.ratings_up == 0
    assert report_a.events_read == 2
    assert report_b.events_read == 1


LEGACY_NOTICE = {
    "id": "hb-legacy000001",
    "kind": KIND_PLAN,
    "text": (
        "It is past 10:00 and there is no plan saved for today yet. "
        "Tell Orbit your plan in chat to save it."
    ),
    "created_at": "2026-10-11T10:05:00Z",
    "dismissed_at": None,
    "push": "off",
    "manual": False,
}


def test_imported_notice_is_not_raised_again_for_the_same_day(tmp_path):
    """One condition, one notice per account per day — on the SQLite store.

    The PR #20 import copied the legacy heartbeat file into the account row.
    When that file held a notice but no matching ``fired`` marker, the next
    scheduler tick opened a second, identical notice. The import now rebuilds
    the marker from the notice, and the tick refuses to duplicate an open one.
    """
    database = Database(tmp_path / "heartbeat.sqlite3")
    manager = SQLiteHeartbeatManager(settings=settings(), database=database)
    heartbeat = manager.for_account("account-A")

    legacy = {**_empty_state(), "last_checkin_at": "2026-10-10T12:00:00Z", "notices": [LEGACY_NOTICE]}
    heartbeat._store.update(lambda state: merge_imported_state(state, legacy))
    assert len(heartbeat.snapshot(at(11, 9, 0))["notices"]) == 1

    assert heartbeat.tick(at(11, 12, 0)) == []
    shown = heartbeat.snapshot(at(11, 12, 0))["notices"]
    assert [n["id"] for n in shown] == [LEGACY_NOTICE["id"]]
    assert heartbeat.tick(at(11, 18, 0)) == []
    assert len(heartbeat.snapshot(at(11, 18, 0))["notices"]) == 1


def test_pre_existing_duplicate_rows_collapse_to_one_without_deleting(tmp_path):
    database = Database(tmp_path / "heartbeat-dup.sqlite3")
    manager = SQLiteHeartbeatManager(settings=settings(), database=database)
    heartbeat = manager.for_account("account-A")
    duplicate = dict(LEGACY_NOTICE, id="hb-legacy000002")
    legacy = {
        **_empty_state(),
        "last_checkin_at": "2026-10-10T12:00:00Z",
        "notices": [LEGACY_NOTICE, duplicate],
    }
    # Written straight into the row, the way the old import left it (two rows,
    # no fired marker) rather than through the merging import.
    heartbeat._store.update(lambda state: state.update(legacy))
    assert len(heartbeat.snapshot(at(11, 9, 0))["notices"]) == 2

    assert heartbeat.tick(at(11, 12, 0)) == []
    assert [n["id"] for n in heartbeat.snapshot(at(11, 12, 0))["notices"]] == [LEGACY_NOTICE["id"]]

    # Both rows are still stored: the second is marked as a copy, not removed.
    with database.connect() as db:
        payload = db.execute(
            "SELECT payload FROM heartbeat_reminders WHERE account_id = ?", ("account-A",)
        ).fetchone()[0]
    import json

    stored = json.loads(payload)["notices"]
    assert len(stored) == 2
    assert stored[1]["duplicate_of"] == LEGACY_NOTICE["id"]
    assert stored[0]["dismissed_at"] is None
