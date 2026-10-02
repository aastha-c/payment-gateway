"""
migrations/initial_schema.sql
------------------------------
Run this against a fresh database.
In production, manage this via Alembic (alembic upgrade head).
"""

-- ============================================================
-- Run as: psql -d payment_gateway -f migrations/initial_schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- idempotency_keys (referenced by transactions via FK)
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    key             VARCHAR(255) NOT NULL,
    request_path    VARCHAR(500) NOT NULL,
    request_hash    VARCHAR(64)  NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing', 'completed', 'failed')),
    response_status INT,
    response_body   JSONB,
    locked_at       TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (key, request_path)
);

CREATE INDEX IF NOT EXISTS idx_idem_key_lookup   ON idempotency_keys(key, request_path);
CREATE INDEX IF NOT EXISTS idx_idem_expires      ON idempotency_keys(expires_at);

-- accounts
CREATE TABLE IF NOT EXISTS accounts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID        NOT NULL,
    currency        CHAR(3)     NOT NULL,
    account_type    VARCHAR(20) NOT NULL
                    CHECK (account_type IN ('asset', 'liability', 'revenue', 'expense', 'equity')),
    balance         BIGINT      NOT NULL DEFAULT 0 CHECK (balance >= 0),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version         BIGINT      NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_accounts_owner    ON accounts(owner_id);

-- transactions
CREATE TABLE IF NOT EXISTS transactions (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key_id  UUID        REFERENCES idempotency_keys(id),
    reference           VARCHAR(255),
    description         TEXT,
    currency            CHAR(3)     NOT NULL,
    total_amount        BIGINT      NOT NULL CHECK (total_amount > 0),
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'failed', 'reversed')),
    metadata            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_status   ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_created  ON transactions(created_at DESC);

-- ledger_entries
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID        NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
    account_id      UUID        NOT NULL REFERENCES accounts(id)     ON DELETE RESTRICT,
    entry_type      VARCHAR(6)  NOT NULL CHECK (entry_type IN ('debit', 'credit')),
    amount          BIGINT      NOT NULL CHECK (amount > 0),
    currency        CHAR(3)     NOT NULL,
    running_balance BIGINT      NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_tx      ON ledger_entries(transaction_id);

-- Double-entry validation trigger
CREATE OR REPLACE FUNCTION check_double_entry_balance()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    debit_sum  BIGINT;
    credit_sum BIGINT;
BEGIN
    IF NEW.status = 'completed' AND OLD.status != 'completed' THEN
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE entry_type = 'debit'),  0),
            COALESCE(SUM(amount) FILTER (WHERE entry_type = 'credit'), 0)
        INTO debit_sum, credit_sum
        FROM ledger_entries
        WHERE transaction_id = NEW.id;

        IF debit_sum != credit_sum THEN
            RAISE EXCEPTION 'Double-entry violation on tx %: debits=% credits=%',
                NEW.id, debit_sum, credit_sum;
        END IF;
        IF debit_sum = 0 THEN
            RAISE EXCEPTION 'Transaction % has no ledger entries', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_double_entry_check ON transactions;
CREATE TRIGGER trg_double_entry_check
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION check_double_entry_balance();
