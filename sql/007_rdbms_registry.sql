-- Slice 2, phase 3 — per-table RDBMS source metadata, sidecar to registry_tables.
--
-- Which stored connection + source schema/table a pipeline pulls from, the date
-- column used for Full/Tenure runs, and an optional static range filter that is
-- ANDed into every extract query so a huge table doesn't come across whole.

CREATE TABLE IF NOT EXISTS registry_rdbms_source (
    source_id      text NOT NULL,
    table_name     text NOT NULL,
    connection_id  text NOT NULL REFERENCES secret.rdbms_connection(connection_id),
    source_schema  text NOT NULL,
    source_table   text NOT NULL,
    tenure_column  text,                       -- date/timestamp col for windowed runs
    static_filter  jsonb,                      -- {column, pg_type, from, to}
    created_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, table_name),
    FOREIGN KEY (source_id, table_name)
        REFERENCES registry_tables(source_id, table_name) ON DELETE CASCADE
);
