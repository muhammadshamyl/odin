-- Waiting-batch resolution gains a third outcome: 'merged' (keep both — insert the
-- held rows into production alongside the existing ones, no delete).

ALTER TABLE waiting_batch_log DROP CONSTRAINT IF EXISTS waiting_batch_log_status_check;
ALTER TABLE waiting_batch_log ADD CONSTRAINT waiting_batch_log_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'merged'));
