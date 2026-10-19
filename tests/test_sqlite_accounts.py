import json

from app.accounts import AccountStore
from app.database import Database
from app.sqlite_accounts import SQLiteAccountStore
from app.sqlite_store import SQLiteStore
from app.store import ChatMessage


EMAIL_A = "ada@example.test"
EMAIL_B = "grace@example.test"


def test_sqlite_account_credentials_and_verification_survive_restart(tmp_path):
    database = Database(tmp_path / "accounts.sqlite3")
    store = SQLiteAccountStore(database, iterations=1_000)
    account = store.register_email("Ada", EMAIL_A, "password1")
    code = store.demo_code(EMAIL_A, "verify")
    assert code is not None

    with database.connect() as db:
        row = db.execute(
            "SELECT email, password_hash, password_salt, "
            "email_verification_token_hash, email_verification_expires_at, account_data "
            "FROM accounts WHERE id = ?",
            (account["id"],),
        ).fetchone()
    assert row["email"] == EMAIL_A
    assert row["password_hash"] != "password1"
    assert row["password_salt"]
    assert row["email_verification_token_hash"]
    assert row["email_verification_token_hash"] != code
    assert row["email_verification_expires_at"]
    serialized = json.dumps(json.loads(row["account_data"]))
    assert code not in serialized
    assert "password1" not in serialized

    reopened = SQLiteAccountStore(Database(database.path), iterations=1_000)
    assert reopened.demo_code(EMAIL_A, "verify") is None
    assert reopened.verify_email(EMAIL_A, code) == "ok"
    assert reopened.check_password(account["id"], "password1")
    assert reopened.by_id(account["id"])["email_verified"] is True


def test_legacy_json_account_data_imports_once_into_sqlite(tmp_path):
    legacy_path = tmp_path / "accounts.json"
    legacy = AccountStore(legacy_path, iterations=1_000)
    account = legacy.register_email("Ada", EMAIL_A, "password1")
    code = legacy.demo_code(EMAIL_A, "verify")
    assert code is not None
    legacy_data = legacy._read()

    database = Database(tmp_path / "accounts.sqlite3")
    sqlite_accounts = SQLiteAccountStore(database, iterations=1_000)
    sqlite_accounts.import_legacy_data(legacy_data, source=str(legacy_path))
    assert sqlite_accounts.check_password(account["id"], "password1")
    assert sqlite_accounts.verify_email(EMAIL_A, code) == "ok"

    sqlite_accounts.delete_account(account["id"])
    sqlite_accounts.import_legacy_data(legacy_data, source=str(legacy_path))
    assert sqlite_accounts.by_email(EMAIL_A) is None


def test_deleting_an_account_keeps_its_owned_workspace_rows_but_removes_login(tmp_path):
    database = Database(tmp_path / "accounts.sqlite3")
    accounts = SQLiteAccountStore(database, iterations=1_000)
    account = accounts.register_email("Ada", EMAIL_A, "password1")
    store = SQLiteStore(str(database.path))
    view = store.for_account(account["id"])
    conversation = view.start_conversation()
    view.append_message(
        conversation.id,
        ChatMessage(who="You", time="10:00", body="kept", is_orbit=False),
    )

    accounts.delete_account(account["id"])
    assert accounts.by_id(account["id"]) is None
    assert accounts.by_email(EMAIL_A) is None
    with database.connect() as db:
        account_row = db.execute(
            "SELECT email, password_hash, is_active FROM accounts WHERE id = ?",
            (account["id"],),
        ).fetchone()
        assert tuple(account_row) == (None, None, 0)
        assert db.execute(
            "SELECT COUNT(*) FROM conversations WHERE account_id = ?", (account["id"],)
        ).fetchone()[0] == 1

    restarted_view = SQLiteStore(str(database.path)).for_account(account["id"])
    assert [message.body for message in restarted_view.thread(conversation.id)] == ["kept"]


def test_sqlite_account_store_keeps_profiles_and_feedback_separate_by_owner(tmp_path):
    database = Database(tmp_path / "accounts.sqlite3")
    store = SQLiteAccountStore(database, iterations=1_000)
    a = store.register_email("Ada", EMAIL_A, "password1")
    b = store.register_email("Grace", EMAIL_B, "password2")
    store.add_feedback(a["id"], "general", "A title", "A body")
    store.add_feedback(b["id"], "bug", "B title", "B body")

    assert len(store.feedback_for(a["id"])) == 1
    assert store.feedback_for(a["id"])[0]["title"] == "A title"
    assert len(store.feedback_for(b["id"])) == 1
    assert store.feedback_for(b["id"])[0]["title"] == "B title"

    restarted = SQLiteAccountStore(Database(database.path), iterations=1_000)
    assert restarted.by_email(EMAIL_A)["name"] == "Ada"
    assert restarted.by_email(EMAIL_B)["name"] == "Grace"
    assert restarted.check_password(a["id"], "password1")
    assert restarted.check_password(b["id"], "password2")
