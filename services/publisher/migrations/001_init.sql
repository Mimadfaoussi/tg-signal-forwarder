CREATE TABLE IF NOT EXISTS signals (
    signal_id          TEXT PRIMARY KEY,
    source_chat_id     BIGINT NOT NULL,
    source_message_id  BIGINT NOT NULL,
    pair               TEXT,
    entries            JSONB,
    take_profits       JSONB,
    stop               NUMERIC,
    stop_note          TEXT,
    signal_date        DATE,
    raw_text           TEXT NOT NULL,
    parse_ok           BOOLEAN NOT NULL DEFAULT TRUE,
    status             TEXT NOT NULL CHECK (status IN ('pending','sent','failed','dry_run')),
    target_chat_id     BIGINT,
    target_message_id  BIGINT,
    attempts           INT NOT NULL DEFAULT 0,
    last_error         TEXT,
    received_at        TIMESTAMPTZ NOT NULL,
    published_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_pair ON signals (pair);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);
