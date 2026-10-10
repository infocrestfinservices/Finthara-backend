"""Builds a project's Excel workbook off the HTTP request and keeps the finished file.

Why: building the workbook (template fill + LibreOffice recalc, plus a one-time SWOT
backfill on older projects) can take longer than the ~100 s the hosting platform lets a
single request run, and the browser then gets a 504 with no CORS header. The browser now
asks for the file to be prepared, polls until it is ready, and downloads the finished file
— the build itself is exactly the same function the old direct download ran, so the
workbook is byte-for-byte the same kind of file.

The finished file is kept on local disk keyed by a fingerprint of everything the workbook
is built from (project fields, stored answers, the report's model). Any edit or
regeneration changes the fingerprint, so a stale file is never served — it is simply
rebuilt. The cache lives in the temp directory, so a redeploy clears it; the next download
builds once and caches again.

Build state ("running" / "failed") is kept in memory: there is one uvicorn process on this
deploy. A process restart loses it, and the browser's poll then sees "none" and asks again.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time

_DIR = os.path.join(tempfile.gettempdir(), "finthara_excel_cache")

# A build still "running" after this long is presumed dead (its worker died) so the user
# can start a new one instead of waiting forever.
_STALE_AFTER_S = 12 * 60

_lock = threading.Lock()
_state: dict[int, dict] = {}   # project_id -> {"status": "running"|"failed", "error", "at"}


def fingerprint(parts) -> str:
    """A stable hash of everything the workbook is built from."""
    blob = json.dumps(parts, default=str, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _paths(project_id: int) -> tuple[str, str]:
    return (os.path.join(_DIR, f"{project_id}.bin"), os.path.join(_DIR, f"{project_id}.json"))


def get(project_id: int, fp: str):
    """(data, filename, media_type) for a cached file built from `fp`, else None."""
    data_path, meta_path = _paths(project_id)
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if meta.get("fp") != fp:
            return None
        with open(data_path, "rb") as fh:
            return fh.read(), meta["fname"], meta["media"]
    except (OSError, ValueError, KeyError):
        return None


def put(project_id: int, fp: str, data: bytes, fname: str, media: str) -> None:
    """Store a finished file. Written to temp names and swapped in, so a reader never sees
    half a file; the meta (which carries the fingerprint) goes last."""
    os.makedirs(_DIR, exist_ok=True)
    data_path, meta_path = _paths(project_id)
    with open(data_path + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(data_path + ".tmp", data_path)
    with open(meta_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"fp": fp, "fname": fname, "media": media}, fh)
    os.replace(meta_path + ".tmp", meta_path)


def begin(project_id: int) -> bool:
    """Mark a build as started. False if one is already running (so a double click or a
    second team member latches onto the build in flight instead of starting another)."""
    with _lock:
        cur = _state.get(project_id)
        if cur and cur["status"] == "running" and time.time() - cur["at"] < _STALE_AFTER_S:
            return False
        _state[project_id] = {"status": "running", "error": None, "at": time.time()}
        return True


def finish(project_id: int, error: str | None = None) -> None:
    with _lock:
        if error:
            _state[project_id] = {"status": "failed", "error": error[:480], "at": time.time()}
        else:
            _state.pop(project_id, None)


def status(project_id: int, fp: str) -> dict:
    """ready (a file for this exact data is cached), running, failed, or none."""
    with _lock:
        cur = dict(_state.get(project_id) or {})
    if cur.get("status") == "running" and time.time() - cur["at"] < _STALE_AFTER_S:
        return {"status": "running"}
    if get(project_id, fp) is not None:
        return {"status": "ready"}
    if cur.get("status") == "failed":
        return {"status": "failed", "error": cur.get("error")}
    return {"status": "none"}
