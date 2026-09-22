ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_status_check;
ALTER TABLE signals ADD CONSTRAINT signals_status_check
    CHECK (status IN ('pending','sent','failed','dry_run','skipped'));
