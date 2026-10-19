from datetime import date, datetime, timezone

from app.analytics import build_analytics
from app.database import Database
from config import HeartbeatSettings
from heartbeat.sqlite_store import SQLiteHeartbeatManager
from tools.registry import AuditLog


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
