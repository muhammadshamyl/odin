"""CSV / TXT connector.

The 'head' of the file path: a CSV/TXT source is *already* a file, so there is
no query and no batch-file rewrite. This module just reads it:

- `read_header`   -> the column names (row 1)
- `sample_rows`   -> the first N data rows, for the onboarding preview
- `iter_batches`  -> the body in row-count batches, for the load

Values come back as text exactly as written. A short row is padded with None; a
long row keeps its extras under the key ``__extra__`` so nothing is silently
dropped (Check 1 will reject the file on a header/width mismatch).
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

_SNIFF_BYTES = 64 * 1024


def detect_format(path: Path) -> str:
    return "CSV" if path.suffix.lower() == ".csv" else "TXT"


def _dialect(path: Path) -> type[csv.Dialect] | csv.Dialect:
    sample = path.read_text(encoding="utf-8", errors="replace")[:_SNIFF_BYTES]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        return csv.excel  # comma default


def read_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, dialect=_dialect(path))
        for row in reader:
            return [c.strip() for c in row]
    raise ValueError(f"{path} is empty — no header row")


def _rows(path: Path, header: list[str]) -> Iterator[dict[str, object]]:
    width = len(header)
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, dialect=_dialect(path))
        next(reader, None)  # skip header
        for raw in reader:
            if not raw:
                continue
            rec: dict[str, object] = {header[i]: (raw[i] if i < len(raw) else None) for i in range(width)}
            if len(raw) > width:
                rec["__extra__"] = raw[width:]
            yield rec


def sample_rows(path: Path, n: int) -> list[dict[str, object]]:
    header = read_header(path)
    out: list[dict[str, object]] = []
    for rec in _rows(path, header):
        out.append(rec)
        if len(out) >= n:
            break
    return out


def iter_batches(path: Path, batch_rows: int) -> Iterator[list[dict[str, object]]]:
    header = read_header(path)
    batch: list[dict[str, object]] = []
    for rec in _rows(path, header):
        batch.append(rec)
        if len(batch) >= batch_rows:
            yield batch
            batch = []
    if batch:
        yield batch
