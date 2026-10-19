CREATE TRIGGER accounts_token_expiry_insert
BEFORE INSERT ON accounts
WHEN (NEW.email_verification_token_hash IS NULL) != (NEW.email_verification_expires_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'verification token hash and expiry must be set together');
END;

CREATE TRIGGER accounts_token_expiry_update
BEFORE UPDATE OF email_verification_token_hash, email_verification_expires_at ON accounts
WHEN (NEW.email_verification_token_hash IS NULL) != (NEW.email_verification_expires_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'verification token hash and expiry must be set together');
END;

-- The legacy payload column is not an account-data escape hatch: credentials
-- must use the dedicated hashed fields above, never plaintext in payload.
CREATE TRIGGER accounts_no_payload_insert
BEFORE INSERT ON accounts
WHEN NEW.payload != '{}'
BEGIN
    SELECT RAISE(ABORT, 'account payload must remain empty');
END;

CREATE TRIGGER accounts_no_payload_update
BEFORE UPDATE OF payload ON accounts
WHEN NEW.payload != '{}'
BEGIN
    SELECT RAISE(ABORT, 'account payload must remain empty');
END;
