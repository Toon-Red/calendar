"""Dream Calendar — multi-calendar schedule storage and event API.

Calendars: personal (one), project-{id} (auto-created from Pipeline Dashboard),
custom-{uuid} (user-created).

Events belong to a calendar and may be sourced from external systems
(pipeline-dashboard tasks, dream goals/todos) or created manually.

Run:
    python app.py
    python app.py --port 5041
"""
import argparse
import http.client
import json
import logging
import os
import socket as _socket
import struct as _struct
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("calendar")

DATA_DIR = Path(__file__).parent / "data"
EVENTS_FILE = DATA_DIR / "events.json"
CALENDARS_FILE = DATA_DIR / "calendars.json"

PIPELINE_DASHBOARD_URL = os.environ.get("PIPELINE_DASHBOARD_URL", "http://127.0.0.1:5100")

DEFAULT_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
]


# ── SO_REUSEADDR + SO_LINGER HTTP client ─────────────────────────────────────
# On Windows, rapid loopback connections leave sockets in TIME_WAIT for ~120 s.
# SO_REUSEADDR alone is insufficient because the OS only honours it for
# explicit bind() calls, not the implicit bind during connect().  SO_LINGER
# with l_linger=0 forces RST on close(), skipping TIME_WAIT entirely and
# freeing the ephemeral port immediately — safe for loopback traffic where
# the response is fully read before close() fires.

class _ReuseAddrHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that sets SO_REUSEADDR + SO_LINGER before connect().

    SO_LINGER with l_onoff=1, l_linger=0 forces an RST on close(),
    bypassing TIME_WAIT and immediately freeing the ephemeral port.
    This eliminates WinError 10048 on rapid loopback connections.
    """

    def connect(self):
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        # Force RST on close → skip TIME_WAIT → free ephemeral port now.
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_LINGER,
                        _struct.pack('ii', 1, 0))
        if self.timeout is not _socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _ReuseAddrHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that uses SO_REUSEADDR-enabled connections."""

    def http_open(self, req):
        return self.do_open(_ReuseAddrHTTPConnection, req)


_reuse_opener = urllib.request.build_opener(_ReuseAddrHTTPHandler)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle — replaces the deprecated on_event("startup")."""
    _bootstrap()
    yield


app = FastAPI(title="Dream Calendar", version="0.2.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_lock = threading.Lock()


# ── Storage ──────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data) -> None:
    """Write *data* as JSON to *path* atomically (write-to-tmp then rename).

    On Windows ``os.replace`` can transiently fail with ``PermissionError``
    when a concurrent reader holds the target file open (Python's default
    ``open()`` does not set ``FILE_SHARE_DELETE``).  Retry with exponential
    back-off so brief overlapping reads (or antivirus/indexer scans on large
    event stores) don't surface as 500s.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        last_err: Exception | None = None
        for attempt in range(10):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.05 * (attempt + 1))  # 50-500ms back-off
        raise last_err  # type: ignore[misc]
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def _load_calendars() -> list[dict]:
    if not CALENDARS_FILE.exists():
        return []
    # On Windows the file may briefly be unavailable during an os.replace()
    # from a concurrent writer, or a reader may see a zero-length / partial
    # file during the rename.  Retry on all transient I/O and parse errors.
    last_err: Exception | None = None
    for attempt in range(10):
        try:
            raw = CALENDARS_FILE.read_text(encoding="utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("empty file", raw, 0)
            return json.loads(raw)
        except (PermissionError, FileNotFoundError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < 9:
                time.sleep(0.05 * (attempt + 1))
            else:
                log.error("_load_calendars: exhausted retries – raising")
                raise OSError("Calendar storage temporarily unavailable") from last_err
    return []  # unreachable — keeps type-checkers happy


def _save_calendars(cals: list[dict]) -> None:
    with _lock:
        _atomic_write(CALENDARS_FILE, cals)


def _load_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    last_err: Exception | None = None
    for attempt in range(10):
        try:
            raw = EVENTS_FILE.read_text(encoding="utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("empty file", raw, 0)
            return json.loads(raw)
        except (PermissionError, FileNotFoundError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < 9:
                time.sleep(0.05 * (attempt + 1))
            else:
                log.error("_load_events: exhausted retries – raising")
                raise OSError("Event storage temporarily unavailable") from last_err
    return []  # unreachable — keeps type-checkers happy


def _save_events(events: list[dict]) -> None:
    with _lock:
        _atomic_write(EVENTS_FILE, events)


# ── Pruning ──────────────────────────────────────────────────────────────────
# The event store grows unbounded — every EOD seed adds ~20 events for the
# next day, completed-task notifications pile up, etc. Daily seeding without
# pruning hit 3963 events in the wild (PD task 08d65305). We keep:
#   * everything dated within the last `keep_days` (default 30) — recent
#     history is useful for standup/EOD lookups.
#   * everything in the future or undated — never prune scheduled work.
#   * everything not in a terminal status (anything except completed /
#     cancelled) — only fully-resolved past events are pruning candidates.
# Pruned events move to a sibling `events_archive.jsonl` file (append-only)
# so a one-shot `python -m calendar.compact` script (or a future archive
# endpoint) can resurrect history if the user really needs it.

EVENTS_ARCHIVE = DATA_DIR / "events_archive.jsonl"


def prune_old_completed_events(keep_days: int = 30,
                                today: Optional[date] = None) -> dict:
    """Move completed/cancelled events older than ``keep_days`` to archive.

    Returns ``{kept, archived, errors}``. Idempotent: re-running with the
    same cutoff is a no-op once the threshold has been swept.
    """
    today = today or date.today()
    from datetime import timedelta
    cutoff = today - timedelta(days=keep_days)
    events = _load_events()
    keep: list[dict] = []
    to_archive: list[dict] = []
    for e in events:
        # Never prune undated, future, or non-terminal events.
        start = (e.get("start") or "")
        date_only = start[:10] if start else None
        terminal = e.get("status") in ("completed", "cancelled")
        if not date_only or not terminal:
            keep.append(e)
            continue
        try:
            ev_date = date.fromisoformat(date_only)
        except ValueError:
            keep.append(e)
            continue
        if ev_date < cutoff:
            to_archive.append(e)
        else:
            keep.append(e)
    if not to_archive:
        return {"kept": len(keep), "archived": 0, "errors": 0}
    # Append to archive jsonl (one event per line) before saving the new
    # truncated event store. If the archive write fails, abort: better to
    # keep events than lose them silently.
    EVENTS_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(EVENTS_ARCHIVE, "a", encoding="utf-8") as f:
            for e in to_archive:
                f.write(json.dumps(e, default=str) + "\n")
    except Exception as exc:
        log.error("calendar prune: archive write failed (%s); aborting prune", exc)
        return {"kept": len(events), "archived": 0,
                "errors": 1, "error": str(exc)}
    _save_events(keep)
    log.info("calendar prune: archived %d, kept %d (cutoff=%s)",
             len(to_archive), len(keep), cutoff.isoformat())
    return {"kept": len(keep), "archived": len(to_archive), "errors": 0}


@app.post("/api/events/prune")
def events_prune(keep_days: int = 30):
    """Trigger the prune+archive pass. Default keep_days=30."""
    return prune_old_completed_events(keep_days=keep_days)


# ── Models ───────────────────────────────────────────────────────────────────

class CalendarCreate(BaseModel):
    name: str
    color: Optional[str] = None
    description: Optional[str] = None


class EventCreate(BaseModel):
    title: str
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: bool = True
    status: str = "scheduled"            # scheduled | completed | cancelled
    source: str = "manual"               # manual | pipeline-dashboard | dream
    source_id: Optional[str] = None
    project_id: Optional[str] = None
    category: str = "task"               # task | deadline | milestone | meeting
    description: Optional[str] = None
    recurring: Optional[str] = None
    trigger_automation: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


def _ensure_calendar(cal_id: str, name: str, ctype: str, *,
                     project_id: Optional[str] = None,
                     color: Optional[str] = None) -> dict:
    """Idempotently create a calendar."""
    cals = _load_calendars()
    for c in cals:
        if c["id"] == cal_id:
            return c
    cal = {
        "id": cal_id,
        "name": name,
        "type": ctype,
        "project_id": project_id,
        "color": color or DEFAULT_PALETTE[len(cals) % len(DEFAULT_PALETTE)],
        "created_at": _now_iso(),
    }
    cals.append(cal)
    _save_calendars(cals)
    return cal


def _calendar_exists(cal_id: str) -> bool:
    return any(c["id"] == cal_id for c in _load_calendars())


def _normalize_event_date(s: str) -> str:
    """Pad bare date to ISO datetime and append UTC if naive — used for sorting/comparison."""
    if "T" not in s:
        s = s + "T00:00:00"
    if not s.endswith("Z") and "+" not in s[10:]:
        s = s + "+00:00"
    return s


def _date_only(s: str) -> str:
    return s[:10]


# ── Startup: bootstrap personal + project calendars, migrate legacy events ──

def _bootstrap():
    # Personal calendar
    _ensure_calendar("personal", "Personal", "personal", color="#3b82f6")

    # Migrate legacy events: ensure calendar_id field
    events = _load_events()
    changed = False
    for e in events:
        if "calendar_id" not in e:
            pid = e.get("project_id")
            e["calendar_id"] = f"project-{pid}" if pid else "personal"
            changed = True
        e.setdefault("status", "scheduled")
        e.setdefault("source", "manual")
        e.setdefault("source_id", None)
        e.setdefault("category", e.get("event_type") or "task")
    if changed:
        _save_events(events)

    # Project calendars from Pipeline Dashboard (best effort).
    # Use _reuse_opener (SO_REUSEADDR) to avoid WinError 10048 when the
    # system has many TIME_WAIT sockets from other Dream loopback traffic.
    try:
        req = urllib.request.Request(f"{PIPELINE_DASHBOARD_URL}/api/projects")
        with _reuse_opener.open(req, timeout=3) as r:
            data = json.loads(r.read())
        projects = data.get("projects", []) if isinstance(data, dict) else data
        for p in projects:
            pid = p.get("project_id")
            name = p.get("name") or pid
            if pid:
                _ensure_calendar(f"project-{pid}", name, "project", project_id=pid)
        log.info("Bootstrapped %d project calendars", len(projects))
    except Exception as e:
        log.warning("Could not fetch projects from Pipeline Dashboard: %s", e)

    # Auto-prune: the event store grows ~20 events/day from EOD seeding.
    # Without pruning the file balloons past 1 MB and increases the
    # contention window for every read-modify-write cycle on Windows.
    try:
        count = len(_load_events())
        if count > 2000:
            log.info("Auto-pruning event store (%d events)", count)
            result = prune_old_completed_events(keep_days=30)
            log.info("Auto-prune result: %s", result)
    except Exception as exc:
        log.warning("Auto-prune failed (non-fatal): %s", exc)


# ── Routes: health + root landing ────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/")
def root():
    """Landing page for the launcher BrowserView.

    Shows a small status panel + counts and links to the API docs.
    Without this the launcher tab loads `/` and gets a generic
    `{"detail":"Not Found"}` instead of a useful page.
    """
    from fastapi.responses import HTMLResponse
    cals = _load_calendars()
    events = _load_events()
    today_iso = _today_iso()
    today_events = [e for e in events if (e.get("start") or "")[:10] == today_iso]
    rows = "".join(
        f"<tr><td>{c.get('id')}</td><td>{c.get('name')}</td><td>{c.get('type')}</td></tr>"
        for c in cals
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dream Calendar</title>
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
h1{{color:#93c5fd;margin:0 0 4px}}
.sub{{color:#64748b;font-size:13px;margin-bottom:24px}}
.stats{{display:flex;gap:16px;margin-bottom:24px}}
.stat{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px 18px;flex:1}}
.stat .num{{font-size:28px;font-weight:700;color:#60a5fa}}
.stat .label{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #334155;font-size:13px}}
th{{background:#0f172a;color:#94a3b8;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
tr:last-child td{{border-bottom:none}}
.links{{margin-top:24px;display:flex;gap:12px}}
a{{color:#60a5fa;text-decoration:none;border:1px solid #334155;padding:6px 12px;border-radius:6px}}
a:hover{{background:#1e293b}}
</style></head>
<body>
<h1>📅 Dream Calendar</h1>
<div class="sub">Multi-calendar schedule storage and event API · v0.2.0</div>
<div class="stats">
  <div class="stat"><div class="num">{len(cals)}</div><div class="label">Calendars</div></div>
  <div class="stat"><div class="num">{len(events)}</div><div class="label">Total events</div></div>
  <div class="stat"><div class="num">{len(today_events)}</div><div class="label">Today</div></div>
</div>
<table><thead><tr><th>ID</th><th>Name</th><th>Type</th></tr></thead><tbody>{rows}</tbody></table>
<div class="links">
  <a href="/docs">API docs</a>
  <a href="/api/events">Events JSON</a>
  <a href="/api/health">Health</a>
</div>
</body></html>"""
    return HTMLResponse(html)


# ── Routes: calendars ────────────────────────────────────────────────────────

@app.get("/api/calendars")
def list_calendars():
    try:
        return _load_calendars()
    except Exception as exc:
        log.error("list_calendars: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.post("/api/calendars", status_code=201)
def create_calendar(body: CalendarCreate):
    try:
        cal_id = f"custom-{uuid.uuid4().hex[:8]}"
        return _ensure_calendar(cal_id, body.name, "custom", color=body.color)
    except Exception as exc:
        log.error("create_calendar: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/calendars/{cal_id}")
def get_calendar(cal_id: str):
    try:
        cals = _load_calendars()
    except Exception as exc:
        log.error("get_calendar(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")
    for c in cals:
        if c["id"] == cal_id:
            return c
    raise HTTPException(404, "Calendar not found")


@app.delete("/api/calendars/{cal_id}")
def delete_calendar(cal_id: str):
    try:
        cals = _load_calendars()
        target = next((c for c in cals if c["id"] == cal_id), None)
        if not target:
            raise HTTPException(404, "Calendar not found")
        if target["type"] != "custom":
            raise HTTPException(400, "Only custom calendars can be deleted")
        _save_calendars([c for c in cals if c["id"] != cal_id])
        # Cascade: drop events on that calendar
        _save_events([e for e in _load_events() if e.get("calendar_id") != cal_id])
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("delete_calendar(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/calendars/{cal_id}/events")
def calendar_events(cal_id: str, from_date: Optional[str] = None, to_date: Optional[str] = None):
    try:
        if not _calendar_exists(cal_id):
            raise HTTPException(404, "Calendar not found")
        events = [e for e in _load_events() if e.get("calendar_id") == cal_id]
        if from_date:
            events = [e for e in events if _date_only(e["start"]) >= from_date]
        if to_date:
            events = [e for e in events if _date_only(e["start"]) <= to_date]
        events.sort(key=lambda e: _normalize_event_date(e["start"]))
        return events
    except HTTPException:
        raise
    except Exception as exc:
        log.error("calendar_events(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/calendars/{cal_id}/timeline")
def calendar_timeline(cal_id: str, days: int = 14):
    """Per-project timeline view: events from today to today + N days,
    grouped by ISO week with per-status summary counts.

    Returns ``{by_week: [{week_start, scheduled, completed, cancelled, events}]}``.
    Reuses ``_filter_events()`` for the date-range filtering.
    """
    try:
        if not _calendar_exists(cal_id):
            raise HTTPException(404, "Calendar not found")

        today = date.today()
        end_date = today + timedelta(days=days)
        events = _filter_events(
            calendar_id=cal_id,
            from_date=today.isoformat(),
            to=end_date.isoformat(),
        )

        # Bucket events by ISO week (Monday-based).
        buckets: dict[str, list[dict]] = {}
        for e in events:
            try:
                ev_date = date.fromisoformat(_date_only(e["start"]))
            except (ValueError, KeyError):
                continue
            # Monday of the event's ISO week
            week_start = ev_date - timedelta(days=ev_date.weekday())
            key = week_start.isoformat()
            buckets.setdefault(key, []).append(e)

        by_week = []
        for week_start in sorted(buckets):
            week_events = buckets[week_start]
            by_week.append({
                "week_start": week_start,
                "scheduled": sum(1 for e in week_events if e.get("status") == "scheduled"),
                "completed": sum(1 for e in week_events if e.get("status") == "completed"),
                "cancelled": sum(1 for e in week_events if e.get("status") == "cancelled"),
                "events": week_events,
            })

        return {"by_week": by_week}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("calendar_timeline(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.post("/api/calendars/{cal_id}/events", status_code=201)
def create_calendar_event(cal_id: str, body: EventCreate):
    """Create an event on the given calendar.

    For external sources (source != 'manual'), this is upsert-by-(source, source_id):
    if an event already exists with the same source+source_id, it's updated in place.
    The calendar is auto-created if it follows the 'project-{id}' pattern.
    """
    # Wrap the *entire* handler — including calendar validation — in a broad
    # try/except so that transient I/O errors (PermissionError from Windows
    # file locking, JSONDecodeError from reading during an atomic swap, etc.)
    # surface as a retryable 503 instead of an opaque 500.
    try:
        if not _calendar_exists(cal_id):
            if cal_id.startswith("project-"):
                pid = cal_id.removeprefix("project-")
                _ensure_calendar(cal_id, pid, "project", project_id=pid)
            else:
                raise HTTPException(404, "Calendar not found")

        payload = body.model_dump()
        payload["start"] = payload.get("start") or _today_iso()

        # Hold the lock across read+modify+write so concurrent POSTs from
        # Dream's fan-out (goal+deliverable+todo all writing at once) don't
        # lose writes.
        with _lock:
            events = _load_events()

            if payload["source"] != "manual" and payload.get("source_id"):
                for e in events:
                    if e.get("source") == payload["source"] and e.get("source_id") == payload["source_id"]:
                        e.update({k: v for k, v in payload.items() if v is not None or k == "status"})
                        e["calendar_id"] = cal_id
                        e["updated_at"] = _now_iso()
                        _atomic_write(EVENTS_FILE, events)
                        return e

            event = {
                "id": uuid.uuid4().hex[:8],
                "calendar_id": cal_id,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                **payload,
            }
            events.append(event)
            _atomic_write(EVENTS_FILE, events)
            return event
    except HTTPException:
        raise
    except Exception as exc:
        log.error("create_calendar_event(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


# ── Routes: events (cross-calendar) ──────────────────────────────────────────

def _filter_events(
    date: Optional[str] = None,
    from_date: Optional[str] = None,
    to: Optional[str] = None,
    project_id: Optional[str] = None,
    source: Optional[str] = None,
    calendar_id: Optional[str] = None,
) -> list[dict]:
    """Plain helper that does the cross-calendar event search.

    Separated from the FastAPI route so internal callers (e.g.
    /api/events/today) can invoke it without the Query() defaults
    binding as Query objects instead of None — that crash was
    PD task 73863fa1.
    """
    events = _load_events()
    if calendar_id:
        events = [e for e in events if e.get("calendar_id") == calendar_id]
    if date:
        events = [e for e in events if _date_only(e["start"]) == date]
    if from_date:
        events = [e for e in events if _date_only(e["start"]) >= from_date]
    if to:
        events = [e for e in events if _date_only(e["start"]) <= to]
    if project_id:
        events = [e for e in events if e.get("project_id") == project_id]
    if source:
        events = [e for e in events if e.get("source") == source]
    events.sort(key=lambda e: _normalize_event_date(e["start"]))
    return events


@app.get("/api/events")
def list_events(
    date: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    project_id: Optional[str] = None,
    source: Optional[str] = None,
    calendar_id: Optional[str] = None,
):
    """Cross-calendar event search.

    Query params:
      date=YYYY-MM-DD          single-day match
      from=DATE&to=DATE        inclusive range
      project_id=X             events for any calendar tied to that project
      source=pipeline-dashboard|dream|manual
      calendar_id=X            scope to one calendar
    """
    try:
        return _filter_events(
            date=date, from_date=from_date, to=to,
            project_id=project_id, source=source, calendar_id=calendar_id,
        )
    except Exception as exc:
        log.error("list_events: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/events/today")
def events_today():
    try:
        return _filter_events(date=_today_iso())
    except Exception as exc:
        log.error("events_today: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/events/{event_id}")
def get_event(event_id: str):
    try:
        events = _load_events()
    except Exception as exc:
        log.error("get_event(%s): %s: %s", event_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")
    for e in events:
        if e["id"] == event_id:
            return e
    raise HTTPException(404, "Event not found")


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    try:
        with _lock:
            events = _load_events()
            remaining = [e for e in events if e["id"] != event_id]
            if len(remaining) == len(events):
                raise HTTPException(404, "Event not found")
            _atomic_write(EVENTS_FILE, remaining)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("delete_event(%s): %s: %s", event_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")
    return {"ok": True}


@app.post("/api/events/{event_id}/complete")
def complete_event(event_id: str):
    try:
        with _lock:
            events = _load_events()
            for e in events:
                if e["id"] == event_id:
                    e["status"] = "completed"
                    e["completed_at"] = _now_iso()
                    e["updated_at"] = _now_iso()
                    _atomic_write(EVENTS_FILE, events)
                    return e
    except HTTPException:
        raise
    except Exception as exc:
        log.error("complete_event(%s): %s: %s", event_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")
    raise HTTPException(404, "Event not found")


@app.post("/api/events/{event_id}/cancel")
def cancel_event(event_id: str):
    try:
        with _lock:
            events = _load_events()
            for e in events:
                if e["id"] == event_id:
                    e["status"] = "cancelled"
                    e["updated_at"] = _now_iso()
                    _atomic_write(EVENTS_FILE, events)
                    return e
    except HTTPException:
        raise
    except Exception as exc:
        log.error("cancel_event(%s): %s: %s", event_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")
    raise HTTPException(404, "Event not found")


# ── Legacy compatibility ─────────────────────────────────────────────────────

@app.get("/api/upcoming")
def upcoming_events(hours: int = 24, trigger_automation: Optional[bool] = None):
    """Events starting within the next N hours — used by Automation."""
    try:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        out = []
        for e in _load_events():
            try:
                start = datetime.fromisoformat(_normalize_event_date(e["start"]).replace("Z", "+00:00"))
                if now <= start <= cutoff:
                    if trigger_automation is None or e.get("trigger_automation") == trigger_automation:
                        out.append(e)
            except Exception:
                pass
        out.sort(key=lambda e: e["start"])
        return out
    except Exception as exc:
        log.error("upcoming_events: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/projects/{project_id}/events")
def project_events(project_id: str, from_date: Optional[str] = None, to_date: Optional[str] = None):
    return list_events(project_id=project_id, from_date=from_date, to=to_date)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def _make_server_socket(host: str, port: int) -> _socket.socket:
    """Create a listening socket with SO_REUSEADDR.

    On Windows, ``asyncio`` does NOT set ``SO_REUSEADDR`` on server sockets
    (unlike Linux where it's the default).  Without it, restarting the
    calendar service after a crash fails with ``WinError 10048``
    (``WSAEADDRINUSE``) because the previous socket lingers in
    ``TIME_WAIT`` for ~120 s.  Pre-creating the socket with the flag set
    eliminates this restart window completely.

    For a local-only service bound to ``127.0.0.1`` the security trade-off
    is negligible — Windows' ``SO_REUSEADDR`` allows address 'hijacking',
    but only other processes on the same machine can exploit it, and they
    already have full access to the loopback interface.
    """
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(2048)
    sock.setblocking(False)
    return sock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5041)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    if args.reload:
        # --reload uses subprocess-based file watching; let uvicorn manage
        # its own sockets so child processes can each bind independently.
        uvicorn.run("app:app", host=args.host, port=args.port, reload=True)
    else:
        # Production path: pre-create socket with SO_REUSEADDR.
        sock = _make_server_socket(args.host, args.port)
        log.info("Listening on %s:%d (SO_REUSEADDR enabled)", args.host, args.port)
        config = uvicorn.Config("app:app")
        server = uvicorn.Server(config)
        server.run(sockets=[sock])


if __name__ == "__main__":
    main()
