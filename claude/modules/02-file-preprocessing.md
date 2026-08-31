# Module 2: File Pre-Processing
**Owner:** Data Engineering (DE)
**Layer:** Pre-Staging
**Back to:** [claude.md](../claude.md)

---

## Purpose

Handles all file-level operations before data enters Staging. Flattens complex file formats (XML, JSON, Shapefile) into tabular structures so that SQL can handle all downstream logic. Also manages the staging file area lifecycle.

---

## Key Design Decisions

- Complex formats are flattened into tabular CSV/TXT before Staging ingestion
- Once flattened, these files are treated identically to RDBMS-sourced flat files
- Flattening is handled via Foundry Pipeline Builder's native transforms (no Python)
- The staging file area is a managed, temporary storage area — files are not kept indefinitely
- Retention default: **1 week**, changeable via settings (D2.1)
- A file that fails to parse is **rejected whole** — no partial ingestion — with an alert, a prompt to the user to re-upload, and the failure reason recorded on the file (D2.3)

---

## Features to Build

### 2.1 XML Flattening
- Uses Pipeline Builder's XML tag extract transform
- Nested elements are flattened into columns
- Repeated elements are exploded into separate rows
- Output: flat CSV with all attributes as column headers

### 2.2 JSON Flattening
- Uses Pipeline Builder's Parse JSON transform
- Nested objects flattened using struct flattening
- Arrays exploded into rows
- Output: flat CSV with all keys as column headers

### 2.3 Shapefile Flattening
- Uses Pipeline Builder's Parse Shapefile transform
- Geometry converted to WKT (Well Known Text) string column
- All attributes become standard columns
- Output: flat CSV with geometry as a VARCHAR column

### 2.4 Nested Struct Flattening
- Applies to any source that produces nested structures
- Uses Flatten Struct transform in Pipeline Builder
- Runs after initial parsing of XML/JSON
- Ensures no nested objects remain before Staging ingestion

### 2.5 Staging File Area Management
- Defined storage path in Foundry for incoming files
- Files organized by: `/{source_system}/{table_name}/{date}/`
- Retention: default 1 week, set via a settings value; a file is removed once its downstream load is confirmed DONE and the retention window has passed
- Processed files flagged in the `staging_file_control` table
- Failed files moved to a separate error path, `error_message` populated, status = FAILED

### 2.6 Malformed File Handling
- Triggered when a parser (XML / JSON / Shapefile) or the CSV/TXT reader cannot process a file
- The entire file is rejected — nothing from it is ingested
- `staging_file_control` row set to FAILED with a human-readable `error_message` (what failed and where)
- An alert is raised and the file surfaces in the self-service File Area view with a **Re-submit file for load** action
- The corrected file is treated as a new landing

---

## Staging File Control Table

```sql
CREATE TABLE staging_file_control (
    file_id             VARCHAR,
    source_system       VARCHAR,
    table_name          VARCHAR,
    file_path           VARCHAR,
    file_format         VARCHAR,   -- CSV, TXT, XML, JSON, SHP
    landing_timestamp   TIMESTAMP,
    processing_status   VARCHAR,   -- PENDING, PROCESSING, DONE, FAILED
    processed_timestamp TIMESTAMP,
    row_count           INTEGER,
    file_size_bytes     INTEGER,
    error_message       VARCHAR
)
```

---

## Flattening Pipeline Flow

```
Landed File (XML/JSON/SHP)
        ↓
Format Detection
        ↓
Appropriate Parser (XML / JSON / SHP)
        ↓
Struct Flattening
        ↓
Array Explosion (if needed)
        ↓
Flat CSV Output
        ↓
Handed to Module 4 (Data Quality - Check 1)
```

---

## Dependencies

- Module 1: Extraction Layer (source of files)
- Module 4: Data Quality (Check 1 runs after pre-processing)
- Module 9: Monitoring (file processing status tracking)

---

## Resolved

- Staging file area retention — 1 week, changeable (D2.1)
- Malformed file — reject whole, alert, prompt re-upload, record reason (D2.3)

## Open Questions

- [ ] Do we archive processed files to cold storage before deletion, or delete outright? (rec: archive to cold ~90 days) — **awaiting confirmation**
