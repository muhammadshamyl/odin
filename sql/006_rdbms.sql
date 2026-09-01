-- Slice 2, phase 1 — RDBMS source connections.
--
-- Credentials live in their own `secret` schema in Odin's Postgres, never in the
-- registry. The password is symmetric-encrypted with pgcrypto; the key is
-- `settings.rdbms_secret_key` (env ODIN_RDBMS_SECRET_KEY).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS secret;

CREATE TABLE IF NOT EXISTS secret.rdbms_connection (
    connection_id   text PRIMARY KEY,
    label           text,                       -- human name ("Warehouse — prod")
    engine          text NOT NULL DEFAULT 'postgres'
                    CHECK (engine IN ('postgres')),   -- mysql: later
    host            text NOT NULL,
    port            integer NOT NULL,
    database        text NOT NULL,
    default_schema  text,                       -- pre-selected on the schema screen
    username        text NOT NULL,
    password_enc    bytea NOT NULL,             -- pgp_sym_encrypt(password, :key)
    ssl_mode        text NOT NULL DEFAULT 'require'
                    CHECK (ssl_mode IN ('disable', 'require')),
    server_version  text,                       -- cached from the successful test
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_ok_at      timestamptz
);
