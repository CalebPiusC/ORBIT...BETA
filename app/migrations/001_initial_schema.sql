CREATE TABLE accounts (
    id TEXT PRIMARY KEY NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX conversations_account_idx ON conversations(account_id);

CREATE TABLE messages (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX messages_account_idx ON messages(account_id);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX tasks_account_idx ON tasks(account_id);

CREATE TABLE projects (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX projects_account_idx ON projects(account_id);

CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX tool_calls_account_idx ON tool_calls(account_id);

CREATE TABLE screen_state (
    account_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE heartbeat_reminders (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX heartbeat_reminders_account_idx ON heartbeat_reminders(account_id);

CREATE TABLE analytics_data (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX analytics_data_account_idx ON analytics_data(account_id);
