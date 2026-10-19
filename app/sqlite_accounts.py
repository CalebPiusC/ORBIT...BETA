"""SQLite-backed authentication/account store for production launches."""
from __future__ import annotations

import copy
import json
import threading
from typing import Any

from app.accounts import AccountStore, DATA_VERSION
from app.database import Database


class SQLiteAccountStore(AccountStore):
    """AccountStore-compatible implementation backed by the accounts table.

    Password hashes and verification-code hashes occupy dedicated columns;
    non-credential account metadata and per-account feedback are JSON encoded
    in account_data. A one-time demo code is held only in process memory by
    the inherited AccountStore implementation.
    """

    def __init__(self, database: Database, *, iterations: int = 120_000) -> None:
        super().__init__(iterations=iterations)
        self.database = database
        self._lock = threading.RLock()

    @staticmethod
    def _read_connection(db) -> dict[str, Any]:
        data: dict[str, Any] = {"version": DATA_VERSION, "accounts": [], "codes": {}, "feedback": []}
        # Accounts are the ownership roots and the only table intentionally
        # read without account_id scoping. Every associated code/feedback item
        # is packed inside its account row, never queried cross-account.
        rows = db.execute(
            "SELECT id, email, password_hash, password_salt, "
            "email_verification_token_hash, email_verification_expires_at, account_data, is_active "
            "FROM accounts ORDER BY rowid"
        ).fetchall()
        for row in rows:
            if not row["email"] or not row["is_active"]:
                # The chat store may have created an ownership-root row for an
                # account that has not registered through the auth flow, or a
                # tombstone retained to preserve owned workspace data after deletion.
                continue
            try:
                stored = json.loads(row["account_data"] or "{}")
            except json.JSONDecodeError:
                stored = {}
            account = dict(stored.get("account") or {})
            account.update(
                id=row["id"],
                email=row["email"],
                password_hash=row["password_hash"] or "",
                password_salt=row["password_salt"] or "",
            )
            data["accounts"].append(account)
            data["feedback"].extend(stored.get("feedback") or [])
            codes = stored.get("codes") or {}
            data["codes"].update(codes)
            token_hash = row["email_verification_token_hash"]
            expiry = row["email_verification_expires_at"]
            verification_meta = stored.get("verification_meta") or {}
            if token_hash and expiry:
                key = f"verify:{row['email']}"
                data["codes"][key] = {
                    "token_hash": token_hash,
                    "salt": verification_meta.get("salt", ""),
                    "purpose": "verify",
                    "email": row["email"],
                    "expires_at": expiry,
                    "attempts_left": verification_meta.get("attempts_left", 0),
                }
        return data

    def _read(self) -> dict[str, Any]:
        with self.database.connect() as db:
            return self._read_connection(db)

    def import_legacy_data(
        self, legacy_data: dict[str, Any], *, source: str = "legacy-account-store"
    ) -> None:
        """Merge a prior JSON AccountStore snapshot once, without plaintext codes."""
        with self.database.connect() as db:
            if db.execute("SELECT 1 FROM data_imports WHERE source = ?", (source,)).fetchone():
                return
        def merge(data: dict[str, Any]) -> None:
            existing = {account["id"] for account in data.get("accounts", [])}
            data.setdefault("accounts", []).extend(
                copy.deepcopy(account)
                for account in legacy_data.get("accounts", [])
                if account.get("id") not in existing
            )
            data.setdefault("codes", {}).update(copy.deepcopy(legacy_data.get("codes", {})))
            feedback_ids = {item.get("id") for item in data.setdefault("feedback", [])}
            data["feedback"].extend(
                copy.deepcopy(item)
                for item in legacy_data.get("feedback", [])
                if item.get("id") not in feedback_ids
            )
        self._update(merge)
        with self.database.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO data_imports(source) VALUES (?)", (source,)
            )

    def _update(self, change):
        with self._lock, self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                before = self._read_connection(db)
                current = json.dumps(before, sort_keys=True, separators=(",", ":"))
                result = change(before)
                after = json.dumps(before, sort_keys=True, separators=(",", ":"))
                if current == after:
                    db.commit()
                    return result

                old_accounts = {item["id"]: item for item in self._read_connection(db)["accounts"]}
                accounts = {item["id"]: item for item in before.get("accounts", [])}
                codes = before.get("codes", {})
                feedback = before.get("feedback", [])
                for account_id in old_accounts.keys() - accounts.keys():
                    # Keep the FK ownership root so workspace data remains
                    # exactly as before account deletion; remove credentials
                    # and mark the auth identity inactive.
                    db.execute(
                        "UPDATE accounts SET email = NULL, password_hash = NULL, "
                        "password_salt = NULL, email_verification_token_hash = NULL, "
                        "email_verification_expires_at = NULL, account_data = '{}', "
                        "is_active = 0 WHERE id = ?",
                        (account_id,),
                    )

                for account_id, account in accounts.items():
                    email = account.get("email") or ""
                    verify_key = f"verify:{email}"
                    verify = codes.get(verify_key)
                    reset_codes = {
                        key: value
                        for key, value in codes.items()
                        if value.get("email") == email and key != verify_key
                    }
                    metadata = {
                        key: value
                        for key, value in account.items()
                        if key not in {"id", "email", "password_hash", "password_salt"}
                    }
                    account_data = {
                        "account": metadata,
                        "codes": reset_codes,
                        "verification_meta": (
                            {"salt": verify.get("salt"), "attempts_left": verify.get("attempts_left")}
                            if verify
                            else {}
                        ),
                        "feedback": [item for item in feedback if item.get("account_id") == account_id],
                    }
                    token_hash = verify.get("token_hash") if verify else None
                    expiry = verify.get("expires_at") if verify else None
                    db.execute(
                        "INSERT INTO accounts(id, email, password_hash, password_salt, "
                        "email_verification_token_hash, email_verification_expires_at, account_data, is_active) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
                        "ON CONFLICT(id) DO UPDATE SET email = excluded.email, "
                        "password_hash = excluded.password_hash, password_salt = excluded.password_salt, "
                        "email_verification_token_hash = excluded.email_verification_token_hash, "
                        "email_verification_expires_at = excluded.email_verification_expires_at, "
                        "account_data = excluded.account_data, is_active = 1 "
                        "WHERE accounts.id = excluded.id",
                        (
                            account_id,
                            email,
                            account.get("password_hash") or None,
                            account.get("password_salt") or None,
                            token_hash,
                            expiry,
                            json.dumps(account_data, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
