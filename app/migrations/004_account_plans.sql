CREATE TABLE plans (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan_date TEXT NOT NULL,
    text TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE INDEX plans_account_date_idx ON plans(account_id, plan_date);
