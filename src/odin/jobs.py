"""In-process background job runner for the web UI.

`extract` and `transform` can run for minutes on a large file; the browser must
not block on them. Jobs run on a **single worker thread** — one pipeline job at a
time, which mirrors the per-table advisory lock and keeps "is the pipeline busy?"
answerable. Job state is in memory only; the durable record is `run_log`.

The CLI keeps calling `extract` / `transform` directly (blocking is fine there).
"""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from odin import extract, runlog, transform

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="odin-job")
_LOCK = threading.Lock()
_JOBS: dict[str, "Job"] = {}
_ORDER: list[str] = []
_MAX_KEEP = 60

_KINDS = ("extract", "transform", "ingest", "ingest_rdbms")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    id: str
    kind: str                      # extract | transform | ingest | ingest_rdbms
    source_id: str
    table_name: str
    file: str | None = None
    tenure_from: str | None = None
    tenure_to: str | None = None
    triggered_by: str = "manual"
    state: str = "queued"          # queued | running | done | failed
    submitted_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result: Any = None
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.kind} · {self.source_id}.{self.table_name}"

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "source_id": self.source_id, "table_name": self.table_name,
            "state": self.state, "error": self.error, "result": self.result,
            "submitted_at": self.submitted_at, "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


def submit(
    kind: str, source_id: str, table_name: str, *,
    file: str | None = None, triggered_by: str = "manual",
    tenure_from: str | None = None, tenure_to: str | None = None,
) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    if kind in ("extract", "ingest") and not file:
        raise ValueError(f"{kind} job needs a file")
    job = Job(id=uuid.uuid4().hex, kind=kind, source_id=source_id,
              table_name=table_name, file=file, triggered_by=triggered_by,
              tenure_from=tenure_from, tenure_to=tenure_to)
    with _LOCK:
        _JOBS[job.id] = job
        _ORDER.append(job.id)
        _trim_locked()
    _POOL.submit(_run, job.id)
    return job.id


def _trim_locked() -> None:
    while len(_ORDER) > _MAX_KEEP:
        oldest = _ORDER[0]
        j = _JOBS.get(oldest)
        if j is None or j.state in ("done", "failed"):
            _ORDER.pop(0)
            _JOBS.pop(oldest, None)
        else:
            break  # don't drop a queued/running job


def _run(job_id: str) -> None:
    job = _JOBS.get(job_id)
    if job is None:
        return
    job.state = "running"
    job.started_at = _now()
    try:
        if job.kind == "extract":
            job.result = extract.run_extract(
                job.source_id, job.table_name, job.file, triggered_by=job.triggered_by
            )
        elif job.kind == "transform":
            job.result = transform.run_transform(
                job.source_id, job.table_name, triggered_by=job.triggered_by
            )
        elif job.kind == "ingest_rdbms":  # pull -> CSV -> load -> transform, ONE run_id
            run_id = runlog.new_run_id()
            ex = extract.run_extract_rdbms(
                job.source_id, job.table_name,
                triggered_by=job.triggered_by, run_id=run_id,
                tenure_from=job.tenure_from, tenure_to=job.tenure_to,
            )
            out: dict[str, Any] = {"run_id": run_id, "extract": ex}
            if ex.get("status") == "loaded":
                out["transform"] = transform.run_transform(
                    job.source_id, job.table_name,
                    triggered_by=job.triggered_by, run_id=run_id,
                )
            job.result = out
        else:  # ingest = extract then (if loaded) transform, under ONE run_id
            run_id = runlog.new_run_id()
            ex = extract.run_extract(
                job.source_id, job.table_name, job.file,
                triggered_by=job.triggered_by, run_id=run_id,
            )
            out: dict[str, Any] = {"run_id": run_id, "extract": ex}
            if ex.get("status") == "loaded":
                out["transform"] = transform.run_transform(
                    job.source_id, job.table_name,
                    triggered_by=job.triggered_by, run_id=run_id,
                )
            job.result = out
        job.state = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced via job.error + run_log
        job.state = "failed"
        job.error = str(exc) or exc.__class__.__name__
        traceback.print_exc()
    finally:
        job.ended_at = _now()


def get(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def recent(limit: int = 20) -> list[Job]:
    with _LOCK:
        ids = list(reversed(_ORDER[-limit:]))
    return [_JOBS[i] for i in ids if i in _JOBS]


def recent_failed(within_seconds: float = 120.0) -> list[Job]:
    """Jobs that failed in the last `within_seconds` — for the runs panel to show
    a red banner, since a failed job otherwise just disappears from `active()`."""
    cutoff = _now().timestamp() - within_seconds
    out = [
        j for j in recent(30)
        if j.state == "failed" and j.ended_at and j.ended_at.timestamp() >= cutoff
    ]
    out.sort(key=lambda j: j.ended_at, reverse=True)
    return out


def active() -> list[Job]:
    with _LOCK:
        ids = list(_ORDER)
    return [_JOBS[i] for i in ids if i in _JOBS and _JOBS[i].state in ("queued", "running")]
