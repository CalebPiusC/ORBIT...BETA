-- Credentials have dedicated hash-only columns. Application code must never
-- write raw passwords or verification tokens to payload or any other field.
ALTER TABLE accounts ADD COLUMN email TEXT;
ALTER TABLE accounts ADD COLUMN password_hash TEXT;
ALTER TABLE accounts ADD COLUMN email_verification_token_hash TEXT;
ALTER TABLE accounts ADD COLUMN email_verification_expires_at TEXT;
CREATE UNIQUE INDEX accounts_email_uq ON accounts(email);

-- Composite candidate keys let SQLite enforce that a child and its parent
-- belong to the same account, not merely that both IDs exist.
CREATE UNIQUE INDEX conversations_account_id_id_uq ON conversations(account_id, id);
CREATE UNIQUE INDEX tasks_account_id_id_uq ON tasks(account_id, id);
CREATE UNIQUE INDEX projects_account_id_id_uq ON projects(account_id, id);

-- Messages belong to exactly one conversation or task, scoped to the same
-- account. Keep owner_id for the stable logical owner ID used by callers.
ALTER TABLE messages RENAME TO messages_v1;
CREATE TABLE messages (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    conversation_id TEXT,
    task_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    CHECK (NOT (conversation_id IS NOT NULL AND task_id IS NOT NULL)),
    FOREIGN KEY (account_id, conversation_id)
        REFERENCES conversations(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY (account_id, task_id)
        REFERENCES tasks(account_id, id) ON DELETE CASCADE
);
INSERT INTO messages (id, account_id, owner_id, payload)
    SELECT id, account_id, owner_id, payload FROM messages_v1;
DROP TABLE messages_v1;
CREATE INDEX messages_account_idx ON messages(account_id);
CREATE UNIQUE INDEX messages_account_id_id_uq ON messages(account_id, id);
