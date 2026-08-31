-- Odin — SQL Console audit log.
-- Applied after 002 by `odin.migrate` (filename order).
--
-- Every statement executed through the in-app SQL Console (web /sql) is recorded
-- here on its own autocommit connection — the same pattern as run_log — so
-- manual surgery on the platform is always traceable, including the statements
-- that were staged in a transaction and then Discarded rather than Committed.

CREATE TABLE IF NOT EXISTS sql_console_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ran_at          timestamptz NOT NULL DEFAULT now(),
    statement_text  text NOT NULL,
    statement_class text NOT NULL
                    CHECK (statement_class IN ('read', 'write', 'ddl', 'admin', 'unknown')),
    read_only       boolean NOT NULL,
    row_count       bigint,
    elapsed_ms      numeric(12,2),
    status          text NOT NULL CHECK (status IN ('ok', 'error', 'discarded')),
    error_message   text,
    by_label        text
);

CREATE INDEX IF NOT EXISTS sql_console_log_ran_at ON sql_console_log (ran_at DESC);
