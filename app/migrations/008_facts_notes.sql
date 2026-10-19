CREATE TABLE facts (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    fact TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX facts_account_fact_uq ON facts(account_id, fact COLLATE NOCASE);
CREATE TABLE notes (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX notes_account_created_idx ON notes(account_id, created_at);
