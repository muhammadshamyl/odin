-- Odin — a schema per physical layer.
-- Applied after 003 by `odin.migrate` (filename order).
--
-- Staging and production tables move out of `public` into a schema named for the
-- layer, so a pipeline's table keeps the exact name the DE gave it:
--
--     staging.<table>   production.<table>   quarantine.<table>   waiting.<table>
--
-- (quarantine + waiting already had their own schemas since 001_core.sql.) The
-- source name is registry metadata, not part of the physical name.
--
-- Forward-only: `registry_tables` stores the resolved name and
-- `odin.naming.qname` treats a dotless name as unqualified (public), so
-- pipelines onboarded before this keep working untouched. New onboards use the
-- schema-qualified names.

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS production;
