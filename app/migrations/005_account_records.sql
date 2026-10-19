ALTER TABLE accounts ADD COLUMN password_salt TEXT;
ALTER TABLE accounts ADD COLUMN account_data TEXT NOT NULL DEFAULT '{}';
