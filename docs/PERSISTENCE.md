# Runtime persistence

Production launchers (`python -m app.server`, `python run_demo.py`, and `python chat.py`) use SQLite at `ORBIT_DB_PATH`, default `~/.orbit/orbit.sqlite3`. The directory is created on startup. Connections are per call, foreign keys are enabled on each connection, SQLite uses WAL and a 10-second busy timeout, and the DB/backup permissions are restricted where supported. `create_app()` and the plain store classes remain in-memory/file-backed when used directly by tests.

## Data inventory

| Data | Production storage | Account-scoped? |
|---|---|---|
| Accounts, password hashes, profile, sessions, hashed verification/reset codes | `accounts` row: credential columns plus `account_data` JSON; password salt is a separate column | Account row is the identity root; each account is separate. Secrets are hashes only. |
| Conversations | `conversations` row; Python conversation record is a pickle payload; project owner is also indexed in `project_id` | Yes; `account_id` FK. |
| Messages | `messages` row; Python message/action-card record is a pickle payload; parent conversation/task IDs are columns | Yes; `account_id` FK and composite parent FKs. |
| Tasks | `tasks` row; Python task/run record is a pickle payload; project/dependency IDs are columns guarded by same-account triggers | Yes; `account_id` FK. |
| Projects | `projects` row with JSON payload | Yes; `account_id` FK. |
| Pending tool calls | `tool_calls` row with JSON tool/argument payload; removed after the owner decides | Yes; `account_id` FK. Resolved calls are represented in conversation action cards and analytics events. |
| Facts | One `facts` row per fact | Yes; `account_id` FK and per-account case-insensitive uniqueness. |
| Notes | One `notes` row per note | Yes; `account_id` FK. |
| Daily plans | One `plans` row per dated plan | Yes; `account_id` FK. |
| Screen state | One `screen_state` row per account; Python state object is a pickle payload | Yes; `account_id` PK/FK. |
| Heartbeat state/reminders | `heartbeat_reminders` row with JSON state (pause, notices, fired dates) | Yes; `account_id` FK. |
| Analytics/audit events | One `analytics_data` row per event with JSON payload | Yes; `account_id` FK; report queries filter by account. |
| Migration versions / one-time import markers | `migrations` and `data_imports` metadata rows | Global schema/import metadata; deliberately not user data. |

The `accounts.payload` legacy column is constrained to `{}`. Password hashes and verification-token hashes/expiry use dedicated fields; other non-secret account metadata is JSON in `account_data`. Verification/reset token plaintext is held only in process memory for the existing beta demo display, never persisted.

## Legacy imports and remaining files

At production startup, existing `runtime/accounts.json`, `runtime/projects.json`, the legacy facts/notes/plans files, and `runtime/heartbeat.json` are imported once using `data_imports` markers. They remain as legacy/rollback sources; the production code reads and writes SQLite after import. Legacy account codes are hashed as part of import. A legacy single-user facts/notes/plans/heartbeat file is attributed to `DEFAULT_USER_ID` because it has no prior account ownership metadata.

No production user data in the inventory above remains authoritative in a global file. Static provider configuration and the unconnected-tools display are process-wide configuration, not user records. Direct test factories intentionally retain in-memory or file-backed defaults so tests remain isolated.
