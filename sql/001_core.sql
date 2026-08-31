-- Odin core schema — Slice 1 (CSV/TXT ingestion)
-- Plain DDL. Applied in filename order by `odin.migrate`.
-- Every table here is cross-source / control-plane. Per-source staging_* /
-- production_* / quarantine.* / waiting.* tables are created at onboarding from
-- the registry (see odin.ddl).

-- ---------------------------------------------------------------------------
-- Schemas for the two per-table diversions
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS quarantine;   -- quarantine.<source>_<table>: VARCHAR mirror of staging
CREATE SCHEMA IF NOT EXISTS waiting;       -- waiting.<source>_<table>:   exact copy of production

-- ---------------------------------------------------------------------------
-- Schema Registry (Module 3)
-- ---------------------------------------------------------------------------

CREATE TABLE registry_sources (
    source_id        text PRIMARY KEY,               -- our slug, never taken from source content
    source_name      text NOT NULL,
    source_type      text NOT NULL
                     CHECK (source_type IN ('FILE_CSV', 'FILE_TXT', 'RDBMS', 'NOSQL')),
    owner            text,
    status           text NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE', 'PAUSED', 'DEPRECATED')),
    registered_date  date NOT NULL DEFAULT current_date,
    last_updated     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry_tables (
    source_id             text NOT NULL REFERENCES registry_sources(source_id),
    table_name            text NOT NULL,
    staging_target        text NOT NULL,             -- staging_<source>_<table>
    production_target     text NOT NULL,             -- production_<source>_<table>
    partition_key         text NOT NULL DEFAULT 'load_date',

    -- collision routing (base build) — DE-configured at onboarding
    load_type             text NOT NULL DEFAULT 'INCREMENTAL'
                          CHECK (load_type IN ('INCREMENTAL', 'FULL_SNAPSHOT')),
    existence_check_column text,                     -- INCREMENTAL only: date column matched (exact) vs production

    -- extraction
    extraction_strategy   text NOT NULL DEFAULT 'FULL'
                          CHECK (extraction_strategy IN ('CURSOR', 'TIME_WINDOW', 'FULL')),
    cursor_column         text,
    window_column         text,
    window_grain          text,
    settling_lag_minutes  integer NOT NULL DEFAULT 0,
    load_recurrence       text NOT NULL DEFAULT 'ONE_TIME'
                          CHECK (load_recurrence IN ('RECURRING', 'ONE_TIME')),

    -- later ("Configure"): row-level keying. NULL in the base build.
    natural_key           text,

    status                text NOT NULL DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE', 'PAUSED', 'FAILED', 'INACTIVE')),
    last_updated          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, table_name)
);

CREATE TABLE registry_columns (
    source_id         text NOT NULL,
    table_name        text NOT NULL,
    column_name       text NOT NULL,
    column_order      integer NOT NULL,
    source_data_type  text,                          -- as seen / declared at source
    target_data_type  text NOT NULL DEFAULT 'text',  -- all text until "Configure" assigns types
    is_nullable       boolean NOT NULL DEFAULT true,
    is_primary_key    boolean NOT NULL DEFAULT false,
    is_watermark      boolean NOT NULL DEFAULT false,
    schema_version    integer NOT NULL DEFAULT 1,
    effective_from    date NOT NULL DEFAULT current_date,
    effective_to      date,                          -- NULL => current
    PRIMARY KEY (source_id, table_name, column_name, schema_version),
    FOREIGN KEY (source_id, table_name) REFERENCES registry_tables(source_id, table_name)
);

CREATE TABLE registry_change_log (
    change_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id         text NOT NULL,
    table_name        text NOT NULL,
    column_name       text,
    change_type       text NOT NULL
                      CHECK (change_type IN ('ADD', 'REMOVE', 'TYPE_CHANGE', 'RENAME', 'REGISTER')),
    old_value         text,
    new_value         text,
    changed_by        text,
    changed_timestamp timestamptz NOT NULL DEFAULT now(),
    schema_version    integer
);

-- ---------------------------------------------------------------------------
-- Staging file area control (Module 2)
-- ---------------------------------------------------------------------------

CREATE TABLE staging_file_control (
    file_id             text PRIMARY KEY,
    source_id           text NOT NULL REFERENCES registry_sources(source_id),
    table_name          text NOT NULL,
    file_path           text NOT NULL,
    file_format         text NOT NULL CHECK (file_format IN ('CSV', 'TXT')),
    landing_timestamp   timestamptz NOT NULL DEFAULT now(),
    processing_status   text NOT NULL DEFAULT 'PENDING'
                        CHECK (processing_status IN ('PENDING', 'LOADING', 'LOADED', 'FAILED')),
    processed_timestamp timestamptz,
    row_count           bigint,
    file_size_bytes     bigint,
    error_message       text
);

-- ---------------------------------------------------------------------------
-- Diversion 1: quarantine — structurally broken rows.
-- The rows live in quarantine.<source>_<table> (created at onboarding, LIKE
-- staging). This control table records the reason + counts per write.
-- ---------------------------------------------------------------------------

CREATE TABLE quarantine_batch_log (
    qbatch_id         text PRIMARY KEY,
    run_id            text,
    source_id         text NOT NULL,
    table_name        text NOT NULL,
    reason            text NOT NULL,                 -- 'over_length', 'empty_required:<col>', 'cast:<col>', ...
    row_count         bigint NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolution_status text NOT NULL DEFAULT 'open'
                      CHECK (resolution_status IN ('open', 'reinjected', 'ignored')),
    resolved_by       text,
    resolved_at       timestamptz
);

CREATE INDEX quarantine_batch_log_open
    ON quarantine_batch_log (created_at)
    WHERE resolution_status = 'open';

-- ---------------------------------------------------------------------------
-- Diversion 2: waiting — rows whose existence-check value already exists in
-- production. The rows live in waiting.<source>_<table> (created at onboarding,
-- LIKE production). This control table records one row per colliding value.
-- ---------------------------------------------------------------------------

CREATE TABLE waiting_batch_log (
    wbatch_id        text PRIMARY KEY,
    run_id           text,
    source_id        text NOT NULL,
    table_name       text NOT NULL,
    existence_value  text NOT NULL,                  -- the colliding value of the existence-check column
    row_count        bigint NOT NULL,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_by      text,
    resolved_at      timestamptz
);

CREATE INDEX waiting_batch_log_pending
    ON waiting_batch_log (created_at)
    WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Observability: run log — one row per (run_id, stage[, batch_id]), written on
-- a separate connection so it survives the transform's whole-run rollback
-- ---------------------------------------------------------------------------

CREATE TABLE run_log (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dag_id             text NOT NULL,
    run_id             text NOT NULL,
    batch_id           text,
    stage              text NOT NULL
                       CHECK (stage IN ('EXTRACT', 'STAGING', 'PRODUCTION', 'BUSINESS', 'MIGRATION')),
    source_id          text,
    table_name         text,
    started_at         timestamptz,
    ended_at           timestamptz,
    status             text NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    rows_processed     bigint,
    rows_to_production bigint,
    rows_to_waiting    bigint,
    rows_quarantined   bigint,
    error_message      text,
    triggered_by       text CHECK (triggered_by IN ('scheduled', 'manual', 'onboarding', 'backfill'))
);

CREATE INDEX run_log_run ON run_log (run_id);
CREATE INDEX run_log_source_table ON run_log (source_id, table_name, started_at DESC);
