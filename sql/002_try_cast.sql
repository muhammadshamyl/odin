-- Odin — portable non-throwing cast test + range domains.
-- Applied after 001_core.sql by `odin.migrate` (filename order).
--
-- The staging -> production transform casts every text field to its declared
-- production type set-based (never row-by-row). Postgres has no built-in
-- non-throwing cast before v16, so we trap the error in a plpgsql helper that
-- is safe to call inside a WHERE clause:
--
--   ... WHERE NOT odin_try_cast(amount::text, 'odin_nonneg_int')   -> cast:amount quarantine
--
-- Range-checked types (nonneg int, [0,1] float) are DOMAINs so the whole
-- check — cast + bound — happens inside the trapped EXECUTE. Nothing a bad
-- value can do escapes as an error.

CREATE OR REPLACE FUNCTION odin_try_cast(v text, t regtype)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN
    IF v IS NULL THEN
        RETURN true;                       -- NULL is not a cast failure; nullability is checked separately
    END IF;
    EXECUTE format('SELECT %L::%s', v, t);
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'odin_nonneg_int') THEN
        CREATE DOMAIN odin_nonneg_int AS integer CHECK (VALUE >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'odin_unit_interval') THEN
        CREATE DOMAIN odin_unit_interval AS double precision CHECK (VALUE >= 0 AND VALUE <= 1);
    END IF;
END $$;
