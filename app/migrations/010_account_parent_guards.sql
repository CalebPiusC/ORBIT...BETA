ALTER TABLE tasks ADD COLUMN project_id TEXT;
ALTER TABLE tasks ADD COLUMN builds_on_id TEXT;
ALTER TABLE conversations ADD COLUMN project_id TEXT;

-- When a referenced parent ID exists, triggers reject it unless the owner is
-- the same account. Unknown IDs are not misclassified as cross-account; the
-- account-scoped Store validates that they exist where its relationship API
-- requires a parent.
CREATE TRIGGER tasks_project_owner_insert
BEFORE INSERT ON tasks
WHEN NEW.project_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id)
 AND NOT EXISTS (SELECT 1 FROM projects WHERE account_id = NEW.account_id AND id = NEW.project_id)
BEGIN
    SELECT RAISE(ABORT, 'task project must belong to the same account');
END;
CREATE TRIGGER tasks_project_owner_update
BEFORE UPDATE OF account_id, project_id ON tasks
WHEN NEW.project_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id)
 AND NOT EXISTS (SELECT 1 FROM projects WHERE account_id = NEW.account_id AND id = NEW.project_id)
BEGIN
    SELECT RAISE(ABORT, 'task project must belong to the same account');
END;

CREATE TRIGGER tasks_dependency_owner_insert
BEFORE INSERT ON tasks
WHEN NEW.builds_on_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM tasks WHERE id = NEW.builds_on_id)
 AND NOT EXISTS (SELECT 1 FROM tasks WHERE account_id = NEW.account_id AND id = NEW.builds_on_id)
BEGIN
    SELECT RAISE(ABORT, 'task dependency must belong to the same account');
END;
CREATE TRIGGER tasks_dependency_owner_update
BEFORE UPDATE OF account_id, builds_on_id ON tasks
WHEN NEW.builds_on_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM tasks WHERE id = NEW.builds_on_id)
 AND NOT EXISTS (SELECT 1 FROM tasks WHERE account_id = NEW.account_id AND id = NEW.builds_on_id)
BEGIN
    SELECT RAISE(ABORT, 'task dependency must belong to the same account');
END;

CREATE TRIGGER conversations_project_owner_insert
BEFORE INSERT ON conversations
WHEN NEW.project_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id)
 AND NOT EXISTS (SELECT 1 FROM projects WHERE account_id = NEW.account_id AND id = NEW.project_id)
BEGIN
    SELECT RAISE(ABORT, 'conversation project must belong to the same account');
END;
CREATE TRIGGER conversations_project_owner_update
BEFORE UPDATE OF account_id, project_id ON conversations
WHEN NEW.project_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id)
 AND NOT EXISTS (SELECT 1 FROM projects WHERE account_id = NEW.account_id AND id = NEW.project_id)
BEGIN
    SELECT RAISE(ABORT, 'conversation project must belong to the same account');
END;
