-- Records how a sent signal's trade ultimately closed, sourced from the
-- execution bot's own status messages in TARGET_CHAT (see PositionTracker).
-- ADD COLUMN IF NOT EXISTS is idempotent against a live table with existing rows.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS realized_pnl_usdt NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trade_closed_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trade_closed_at TIMESTAMPTZ;
