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

_STARTED_AT: Optional[datetime] = None  # set during lifespan startup

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
    global _STARTED_AT
    _STARTED_AT = datetime.now(timezone.utc)
    _bootstrap()
    # 2026-05-25 (SCHEDULER-LIFESPAN-WIRE1): actually start the
    # scheduler thread. 994deef5 shipped the scheduler module +
    # orchestration events (eod-daily, sod-daily, wiki-health-hourly)
    # but never wired start_loop() into the service lifecycle, so
    # every scheduled orchestration has been silently dormant since.
    # Evidence: data/orchestration-runs/ dir never created;
    # pipeline-dashboard EOD last fired 2026-05-23.
    import scheduler as sched
    sched.start_loop(load_events=_load_events)
    try:
        yield
    finally:
        sched.stop_loop()


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
    from test_isolation import assert_safe_persist  # a80ad60d Phase 3
    assert_safe_persist(path)
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


class CalendarUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    archived: Optional[bool] = None


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
    # 994deef5: optional orchestration trigger. When present, the
    # calendar scheduler treats this event as a scheduled job. See
    # scheduler.py for the shape contract + fire rules.
    orchestration: Optional[dict] = None


class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: Optional[bool] = None
    status: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    recurring: Optional[str] = None
    orchestration: Optional[dict] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


def _ensure_calendar(cal_id: str, name: str, ctype: str, *,
                     project_id: Optional[str] = None,
                     color: Optional[str] = None,
                     description: Optional[str] = None,
                     archived: bool = False) -> dict:
    """Idempotently create a calendar.

    If a calendar with *cal_id* already exists, its display name is
    refreshed when the caller supplies a different *name*.  This lets
    ``_bootstrap()`` correct stale names (e.g. a calendar auto-created
    by ``create_calendar_event`` with a raw slug like 'minecraft-server'
    gets its proper Pipeline Dashboard display name on the next restart).

    The *archived* flag controls visibility: archived calendars are hidden
    from the default ``GET /api/calendars`` response (use ``include_archived=true``
    to see them).  During bootstrap, this flag is synced from the Pipeline
    Dashboard project's ``hidden`` field.
    """
    cals = _load_calendars()
    for c in cals:
        if c["id"] == cal_id:
            changed = False
            # Refresh display name if the authoritative source changed.
            if name and c.get("name") != name:
                c["name"] = name
                changed = True
            # Sync archived flag from authoritative source.
            if c.get("archived", False) != archived:
                c["archived"] = archived
                changed = True
            if changed:
                _save_calendars(cals)
            return c
    cal = {
        "id": cal_id,
        "name": name,
        "type": ctype,
        "project_id": project_id,
        "color": color or DEFAULT_PALETTE[len(cals) % len(DEFAULT_PALETTE)],
        "description": description,
        "archived": archived,
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


def _event_overlaps_date(event: dict, target_date: str) -> bool:
    """Return True if *event* overlaps with *target_date* (YYYY-MM-DD).

    Overlap logic:
      • start_date == target_date → yes (event starts today)
      • start_date < target_date AND end_date >= target_date → yes (multi-day
        event that spans into today)
      • Otherwise → no

    Events without a start field are excluded. Events without an end field
    (or where end == start) are treated as single-day events — they only
    match when their start date equals target_date.

    This is the ONE canonical definition of 'events on a date' — the
    landing page, /api/events/today, and any future consumer must use this
    helper so counts always agree.
    """
    start_raw = event.get("start") or ""
    start = start_raw[:10]
    if not start or len(start) < 10:
        return False
    if start == target_date:
        return True
    # Multi-day: event started before target but hasn't ended yet.
    end_raw = event.get("end") or ""
    end = end_raw[:10]
    if end and len(end) >= 10 and start < target_date <= end:
        return True
    return False


def _events_overlapping_date(target_date: str) -> list[dict]:
    """Return all events that overlap *target_date*, sorted by start time.

    This is the single source of truth for 'today' counts across the app.
    """
    events = _load_events()
    matched = [e for e in events if _event_overlaps_date(e, target_date)]
    matched.sort(key=lambda e: _normalize_event_date(e.get("start", "")))
    return matched


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
        valid_pids: set[str] = set()
        for p in projects:
            pid = p.get("project_id")
            name = p.get("name") or pid
            if pid:
                valid_pids.add(pid)
                # Sync the archived flag from PD's project visibility.
                # PD marks deprecated/retired projects with hidden=true;
                # we mirror that as archived=true so they don't clutter
                # the calendar list or Dream's pills.
                is_archived = bool(p.get("hidden", False))
                _ensure_calendar(f"project-{pid}", name, "project",
                                 project_id=pid, archived=is_archived)

        # Prune project calendars whose project_id is NOT in Pipeline
        # Dashboard.  This cleans up test-fixture leaks (proj1, proj2)
        # and stale entries from projects that have been removed.
        cals = _load_calendars()
        stale = [c for c in cals
                 if c.get("type") == "project"
                 and c.get("project_id")
                 and c["project_id"] not in valid_pids]
        if stale:
            stale_ids = {c["id"] for c in stale}
            log.info("Pruning %d stale project calendars: %s",
                     len(stale), ", ".join(sorted(stale_ids)))
            _save_calendars([c for c in cals if c["id"] not in stale_ids])
            # Also drop any orphaned events on those calendars.
            events = _load_events()
            orphaned = [e for e in events if e.get("calendar_id") in stale_ids]
            if orphaned:
                log.info("Dropping %d orphaned events from pruned calendars", len(orphaned))
                _save_events([e for e in events if e.get("calendar_id") not in stale_ids])

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
    now = datetime.now(timezone.utc)
    uptime_seconds = (now - _STARTED_AT).total_seconds() if _STARTED_AT else 0
    return {
        "status": "ok",
        "version": "0.2.0",
        "started_at": _STARTED_AT.isoformat() if _STARTED_AT else None,
        "uptime_seconds": int(uptime_seconds),
    }


@app.get("/api/ready")
def readiness():
    """Deep readiness check — verifies the data layer is accessible.

    Unlike /api/health (which confirms the HTTP server is up), this endpoint
    actually reads calendars and events to confirm the service can serve data.
    Used by the EOD UI playtest and watchdog --json to distinguish between
    'TCP port is open' and 'service is fully operational'.
    """
    checks: dict[str, str] = {}
    ok = True

    # Check calendars file
    try:
        cals = _load_calendars()
        checks["calendars"] = f"ok ({len(cals)} loaded)"
    except Exception as exc:
        checks["calendars"] = f"error: {exc}"
        ok = False

    # Check events file
    try:
        events = _load_events()
        checks["events"] = f"ok ({len(events)} loaded)"
    except Exception as exc:
        checks["events"] = f"error: {exc}"
        ok = False

    # Check data directory is writable
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        test_file = DATA_DIR / ".readiness_probe"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        checks["data_writable"] = "ok"
    except Exception as exc:
        checks["data_writable"] = f"error: {exc}"
        ok = False

    status_code = 200 if ok else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status_code,
        content={
            "ready": ok,
            "checks": checks,
            "version": "0.2.0",
            "started_at": _STARTED_AT.isoformat() if _STARTED_AT else None,
        },
    )


@app.get("/")
def root():
    """Landing page for the launcher BrowserView.

    Interactive SPA that supports:
      - Calendar list with per-calendar event counts
      - Click-to-drill-down event browser per calendar
      - Full CRUD: create, edit, delete events; rename/delete calendars
    """
    from fastapi.responses import HTMLResponse
    all_cals = _load_calendars()
    # Only show non-archived calendars on the landing page
    cals = [c for c in all_cals if not c.get("archived", False)]
    events = _load_events()
    today_iso = _today_iso()
    today_events = _events_overlapping_date(today_iso)

    # Pre-compute event counts per calendar for the initial server render
    cal_event_counts: dict[str, int] = {}
    for e in events:
        cid = e.get("calendar_id", "")
        cal_event_counts[cid] = cal_event_counts.get(cid, 0) + 1

    # Pre-serialize to JSON for safe injection into the template
    import json as _json
    cals_json = _json.dumps(cals, default=str)
    cal_counts_json = _json.dumps(cal_event_counts)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dream Calendar</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
h1{{color:#93c5fd;margin:0 0 4px}}
.sub{{color:#64748b;font-size:13px;margin-bottom:24px}}
.stats{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
.stat{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px 18px;flex:1;min-width:120px}}
.stat .num{{font-size:28px;font-weight:700;color:#60a5fa}}
.stat .label{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #334155;font-size:13px}}
th{{background:#0f172a;color:#94a3b8;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
tr:last-child td{{border-bottom:none}}
.links{{margin-top:24px;display:flex;gap:12px;flex-wrap:wrap}}
a,.btn{{color:#60a5fa;text-decoration:none;border:1px solid #334155;padding:6px 12px;border-radius:6px;cursor:pointer;background:transparent;font-size:13px;display:inline-block}}
a:hover,.btn:hover{{background:#1e293b}}
.btn-primary{{background:#3b82f6;color:#fff;border-color:#3b82f6}}
.btn-primary:hover{{background:#2563eb}}
.btn-danger{{color:#ef4444;border-color:#ef4444}}
.btn-danger:hover{{background:#7f1d1d}}
.btn-success{{color:#10b981;border-color:#10b981}}
.btn-success:hover{{background:#064e3b}}
.btn-warning{{color:#f59e0b;border-color:#f59e0b}}
.btn-warning:hover{{background:#78350f}}
.btn-sm{{padding:3px 8px;font-size:12px}}
/* Calendar list */
.cal-row{{cursor:pointer;transition:background .15s}}
.cal-row:hover{{background:#334155}}
.cal-color{{width:12px;height:12px;border-radius:3px;display:inline-block;margin-right:8px;vertical-align:middle}}
.cal-count{{color:#64748b;font-size:12px}}
/* Toolbar */
.toolbar{{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
.toolbar h2{{margin:0;color:#93c5fd;font-size:18px;flex:1}}
/* Status badges */
.badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em}}
.badge-scheduled{{background:#1e3a5f;color:#60a5fa}}
.badge-completed{{background:#064e3b;color:#10b981}}
.badge-cancelled{{background:#7f1d1d;color:#ef4444}}
/* Modal */
.modal-bg{{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}}
.modal-bg.open{{display:flex}}
.modal{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto}}
.modal h3{{margin:0 0 16px;color:#93c5fd}}
.field{{margin-bottom:14px}}
.field label{{display:block;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}}
.field input,.field select,.field textarea{{width:100%;padding:8px 10px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:13px;font-family:inherit}}
.field textarea{{resize:vertical;min-height:60px}}
.field input:focus,.field select:focus,.field textarea:focus{{outline:none;border-color:#3b82f6}}
.field-row{{display:flex;gap:12px}}
.field-row .field{{flex:1}}
.modal-actions{{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}}
/* Empty state */
.empty{{text-align:center;padding:40px 20px;color:#64748b}}
.empty-icon{{font-size:32px;margin-bottom:8px}}
/* Toast */
.toast{{position:fixed;bottom:24px;right:24px;background:#10b981;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;z-index:200;opacity:0;transform:translateY(10px);transition:all .3s}}
.toast.show{{opacity:1;transform:translateY(0)}}
.toast.error{{background:#ef4444}}
/* Actions cell */
.actions{{white-space:nowrap}}
.actions .btn-sm{{margin-right:4px}}
</style></head>
<body>

<h1>\U0001f4c5 Dream Calendar</h1>
<div class="sub">Multi-calendar schedule storage and event API · v0.2.0</div>

<!-- Stats -->
<div class="stats">
  <div class="stat"><div class="num" id="stat-cals">{len(cals)}</div><div class="label">Calendars</div></div>
  <div class="stat"><div class="num" id="stat-events">{len(events)}</div><div class="label">Total events</div></div>
  <div class="stat"><div class="num" id="stat-today">{len(today_events)}</div><div class="label">Today</div></div>
</div>

<!-- View: Calendar List -->
<div id="view-calendars">
  <div class="toolbar">
    <h2>Calendars</h2>
    <button class="btn btn-primary" onclick="showCreateCalendar()">+ New Calendar</button>
  </div>
  <table>
    <thead><tr><th></th><th>Name</th><th>Type</th><th>Events</th><th></th></tr></thead>
    <tbody id="cal-tbody"></tbody>
  </table>
</div>

<!-- View: Calendar Events -->
<div id="view-events" style="display:none">
  <div class="toolbar">
    <button class="btn" onclick="showCalendarList()">← Back</button>
    <h2 id="events-title">Events</h2>
    <button class="btn btn-primary" onclick="showCreateEvent()">+ New Event</button>
  </div>
  <table>
    <thead><tr><th>Title</th><th>Start</th><th>End</th><th>Status</th><th>Category</th><th>Source</th><th>Actions</th></tr></thead>
    <tbody id="events-tbody"></tbody>
  </table>
</div>

<!-- Modal: Event Form -->
<div class="modal-bg" id="modal-event">
  <div class="modal">
    <h3 id="event-form-title">New Event</h3>
    <form id="event-form" onsubmit="return saveEvent(event)">
      <input type="hidden" id="ef-id">
      <input type="hidden" id="ef-cal-id">
      <div class="field">
        <label>Title</label>
        <input id="ef-title" required>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Start</label>
          <input id="ef-start" type="datetime-local">
        </div>
        <div class="field">
          <label>End</label>
          <input id="ef-end" type="datetime-local">
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Category</label>
          <select id="ef-category">
            <option value="task">Task</option>
            <option value="deadline">Deadline</option>
            <option value="milestone">Milestone</option>
            <option value="meeting">Meeting</option>
          </select>
        </div>
        <div class="field">
          <label>Status</label>
          <select id="ef-status">
            <option value="scheduled">Scheduled</option>
            <option value="completed">Completed</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>
      </div>
      <div class="field">
        <label>Description</label>
        <textarea id="ef-desc" rows="3"></textarea>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" onclick="closeModal('modal-event')">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  </div>
</div>

<!-- Modal: Calendar Form -->
<div class="modal-bg" id="modal-calendar">
  <div class="modal">
    <h3 id="cal-form-title">New Calendar</h3>
    <form id="cal-form" onsubmit="return saveCalendar(event)">
      <input type="hidden" id="cf-id">
      <div class="field">
        <label>Name</label>
        <input id="cf-name" required>
      </div>
      <div class="field">
        <label>Color</label>
        <input id="cf-color" type="color" value="#3b82f6">
      </div>
      <div class="field">
        <label>Description</label>
        <textarea id="cf-desc" rows="2"></textarea>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" onclick="closeModal('modal-calendar')">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<!-- Footer links -->
<div class="links">
  <a href="/docs">API docs</a>
  <a href="/api/events">Events JSON</a>
  <a href="/api/health">Health</a>
</div>

<script>
const API = '';
let currentCalId = null;
let calendars = [];
let calEventCounts = {cal_counts_json};

// ── Utilities ──────────────────────────────────────────────────
function esc(s) {{
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}}

function toast(msg, isError) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => t.className = 'toast', 3000);
}}

function closeModal(id) {{
  document.getElementById(id).classList.remove('open');
}}

function openModal(id) {{
  document.getElementById(id).classList.add('open');
}}

async function api(path, opts) {{
  try {{
    const resp = await fetch(API + path, opts);
    if (!resp.ok) {{
      const err = await resp.json().catch(() => ({{}}));
      throw new Error(err.detail || `HTTP ${{resp.status}}`);
    }}
    return resp.json();
  }} catch(e) {{
    toast(e.message, true);
    throw e;
  }}
}}

function fmtDate(s) {{
  if (!s) return '—';
  try {{
    const d = new Date(s);
    if (isNaN(d)) return esc(s.substring(0, 16).replace('T', ' '));
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}});
  }} catch(_) {{ return esc(s.substring(0, 16)); }}
}}

function badgeFor(status) {{
  const cls = {{'scheduled':'badge-scheduled','completed':'badge-completed','cancelled':'badge-cancelled'}}[status] || 'badge-scheduled';
  return `<span class="badge ${{cls}}">${{esc(status || 'scheduled')}}</span>`;
}}

// ── Calendar List ──────────────────────────────────────────────
async function loadCalendars() {{
  calendars = await api('/api/calendars');  // server already excludes archived
  renderCalendars();
  // Refresh stats
  const h = await api('/api/health');
  const events = await api('/api/events');
  const tc = await api('/api/events/today/count');
  document.getElementById('stat-cals').textContent = calendars.length;
  document.getElementById('stat-events').textContent = events.length;
  document.getElementById('stat-today').textContent = tc.count;
  // Rebuild calEventCounts
  calEventCounts = {{}};
  events.forEach(e => {{ calEventCounts[e.calendar_id] = (calEventCounts[e.calendar_id]||0) + 1; }});
  renderCalendars();
}}

function renderCalendars() {{
  const tbody = document.getElementById('cal-tbody');
  if (!calendars.length) {{
    tbody.innerHTML = '<tr><td colspan="5"><div class="empty"><div class="empty-icon">\U0001f4c5</div>No calendars yet</div></td></tr>';
    return;
  }}
  tbody.innerHTML = calendars.map(c => `
    <tr class="cal-row" onclick="drillDown(this.dataset.id,this.dataset.name)" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}">
      <td><span class="cal-color" style="background:${{esc(c.color || '#3b82f6')}}"></span></td>
      <td>${{esc(c.name)}}</td>
      <td>${{esc(c.type)}}</td>
      <td><span class="cal-count">${{calEventCounts[c.id] || 0}}</span></td>
      <td class="actions" onclick="event.stopPropagation()">
        <button class="btn btn-sm" onclick="showEditCalendar(this.dataset.id)" data-id="${{esc(c.id)}}">Rename</button>
        ${{c.type === 'custom' ? `<button class="btn btn-sm btn-danger" onclick="deleteCalendar(this.dataset.id,this.dataset.name)" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}">Delete</button>` : ''}}
      </td>
    </tr>
  `).join('');
}}

function showCalendarList() {{
  document.getElementById('view-calendars').style.display = '';
  document.getElementById('view-events').style.display = 'none';
  currentCalId = null;
  loadCalendars();
}}

// ── Calendar CRUD ──────────────────────────────────────────────
function showCreateCalendar() {{
  document.getElementById('cal-form-title').textContent = 'New Calendar';
  document.getElementById('cf-id').value = '';
  document.getElementById('cf-name').value = '';
  document.getElementById('cf-color').value = '#3b82f6';
  document.getElementById('cf-desc').value = '';
  openModal('modal-calendar');
}}

function showEditCalendar(calId) {{
  const cal = calendars.find(c => c.id === calId);
  if (!cal) return;
  document.getElementById('cal-form-title').textContent = 'Edit Calendar';
  document.getElementById('cf-id').value = calId;
  document.getElementById('cf-name').value = cal.name;
  document.getElementById('cf-color').value = cal.color || '#3b82f6';
  document.getElementById('cf-desc').value = cal.description || '';
  openModal('modal-calendar');
}}

async function saveCalendar(ev) {{
  ev.preventDefault();
  const calId = document.getElementById('cf-id').value;
  const name = document.getElementById('cf-name').value.trim();
  const color = document.getElementById('cf-color').value;
  const description = document.getElementById('cf-desc').value.trim() || null;
  if (!name) return;

  try {{
    if (calId) {{
      await api(`/api/calendars/${{calId}}`, {{
        method: 'PUT',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{name, color, description}})
      }});
      toast('Calendar updated');
    }} else {{
      await api('/api/calendars', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{name, color, description}})
      }});
      toast('Calendar created');
    }}
    closeModal('modal-calendar');
    loadCalendars();
  }} catch(_) {{}}
  return false;
}}

async function deleteCalendar(calId, name) {{
  if (!confirm(`Delete calendar "${{name}}" and all its events?`)) return;
  try {{
    await api(`/api/calendars/${{calId}}`, {{method:'DELETE'}});
    toast('Calendar deleted');
    loadCalendars();
  }} catch(_) {{}}
}}

// ── Event Browser ──────────────────────────────────────────────
async function drillDown(calId, calName) {{
  currentCalId = calId;
  document.getElementById('view-calendars').style.display = 'none';
  document.getElementById('view-events').style.display = '';
  document.getElementById('events-title').textContent = calName + ' — Events';
  await loadEvents();
}}

async function loadEvents() {{
  if (!currentCalId) return;
  const events = await api(`/api/calendars/${{currentCalId}}/events`);
  renderEvents(events);
}}

function renderEvents(events) {{
  const tbody = document.getElementById('events-tbody');
  if (!events.length) {{
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="empty-icon">\U0001f4cb</div>No events in this calendar</div></td></tr>';
    return;
  }}
  tbody.innerHTML = events.map(e => `
    <tr${{e.description ? ` title="${{esc(e.description)}}"` : ''}}>
      <td>${{esc(e.title)}}</td>
      <td>${{fmtDate(e.start)}}</td>
      <td>${{fmtDate(e.end)}}</td>
      <td>${{badgeFor(e.status)}}</td>
      <td>${{esc(e.category || 'task')}}</td>
      <td>${{esc(e.source || 'manual')}}</td>
      <td class="actions">
        <button class="btn btn-sm" onclick="showEditEvent(this.dataset.id)" data-id="${{esc(e.id)}}">Edit</button>
        ${{e.status !== 'completed' ? `<button class="btn btn-sm btn-success" onclick="completeEvent(this.dataset.id)" data-id="${{esc(e.id)}}">Complete</button>` : ''}}
        ${{e.status !== 'cancelled' ? `<button class="btn btn-sm btn-warning" onclick="cancelEvent(this.dataset.id)" data-id="${{esc(e.id)}}">Cancel</button>` : ''}}
        <button class="btn btn-sm btn-danger" onclick="deleteEvent(this.dataset.id,this.dataset.title)" data-id="${{esc(e.id)}}" data-title="${{esc(e.title)}}">Delete</button>
      </td>
    </tr>
  `).join('');
}}

// ── Event CRUD ─────────────────────────────────────────────────
function isoLocal(s) {{
  // Convert ISO string to datetime-local value
  if (!s) return '';
  return s.substring(0, 16);
}}

function showCreateEvent() {{
  document.getElementById('event-form-title').textContent = 'New Event';
  document.getElementById('ef-id').value = '';
  document.getElementById('ef-cal-id').value = currentCalId;
  document.getElementById('ef-title').value = '';
  document.getElementById('ef-start').value = '';
  document.getElementById('ef-end').value = '';
  document.getElementById('ef-category').value = 'task';
  document.getElementById('ef-status').value = 'scheduled';
  document.getElementById('ef-desc').value = '';
  openModal('modal-event');
}}

async function showEditEvent(eventId) {{
  try {{
    const e = await api(`/api/events/${{eventId}}`);
    document.getElementById('event-form-title').textContent = 'Edit Event';
    document.getElementById('ef-id').value = e.id;
    document.getElementById('ef-cal-id').value = e.calendar_id;
    document.getElementById('ef-title').value = e.title || '';
    document.getElementById('ef-start').value = isoLocal(e.start);
    document.getElementById('ef-end').value = isoLocal(e.end);
    document.getElementById('ef-category').value = e.category || 'task';
    document.getElementById('ef-status').value = e.status || 'scheduled';
    document.getElementById('ef-desc').value = e.description || '';
    openModal('modal-event');
  }} catch(_) {{}}
}}

async function saveEvent(ev) {{
  ev.preventDefault();
  const eventId = document.getElementById('ef-id').value;
  const calId = document.getElementById('ef-cal-id').value;
  const title = document.getElementById('ef-title').value.trim();
  const start = document.getElementById('ef-start').value;
  const end = document.getElementById('ef-end').value;
  const category = document.getElementById('ef-category').value;
  const status = document.getElementById('ef-status').value;
  const description = document.getElementById('ef-desc').value;
  if (!title) return false;

  const payload = {{ title, category, status, description: description || null, all_day: !start.includes('T') }};
  if (start) payload.start = start;
  if (end) payload.end = end;

  try {{
    if (eventId) {{
      await api(`/api/events/${{eventId}}`, {{
        method: 'PUT',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(payload)
      }});
      toast('Event updated');
    }} else {{
      await api(`/api/calendars/${{calId}}/events`, {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(payload)
      }});
      toast('Event created');
    }}
    closeModal('modal-event');
    loadEvents();
  }} catch(_) {{}}
  return false;
}}

async function deleteEvent(eventId, title) {{
  if (!confirm(`Delete event "${{title}}"?`)) return;
  try {{
    await api(`/api/events/${{eventId}}`, {{method:'DELETE'}});
    toast('Event deleted');
    loadEvents();
  }} catch(_) {{}}
}}

async function completeEvent(eventId) {{
  try {{
    await api(`/api/events/${{eventId}}/complete`, {{method:'POST'}});
    toast('Event completed');
    loadEvents();
  }} catch(_) {{}}
}}

async function cancelEvent(eventId) {{
  try {{
    await api(`/api/events/${{eventId}}/cancel`, {{method:'POST'}});
    toast('Event cancelled');
    loadEvents();
  }} catch(_) {{}}
}}

// ── Modal keyboard & click-outside handlers ───────────────────
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') {{
    document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
  }}
}});
document.querySelectorAll('.modal-bg').forEach(bg => {{
  bg.addEventListener('click', function(e) {{
    if (e.target === bg) bg.classList.remove('open');
  }});
}});

// ── Init ───────────────────────────────────────────────────────
renderCalendars();
// Bootstrap calendar data from server-rendered JSON
calendars = {cals_json};
renderCalendars();
</script>
</body></html>"""

    return HTMLResponse(html)


# ── Routes: calendars ────────────────────────────────────────────────────────

@app.get("/api/calendars")
def list_calendars(include_archived: bool = False):
    """List all calendars.

    By default, archived (deprecated/hidden) calendars are excluded.
    Pass ``include_archived=true`` to see everything.
    """
    try:
        cals = _load_calendars()
        if not include_archived:
            cals = [c for c in cals if not c.get("archived", False)]
        return cals
    except Exception as exc:
        log.error("list_calendars: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.post("/api/calendars", status_code=201)
def create_calendar(body: CalendarCreate):
    try:
        cal_id = f"custom-{uuid.uuid4().hex[:8]}"
        return _ensure_calendar(cal_id, body.name, "custom", color=body.color,
                                description=body.description)
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


@app.put("/api/calendars/{cal_id}")
def update_calendar(cal_id: str, body: CalendarUpdate):
    """Rename, recolour, or archive/unarchive a calendar."""
    try:
        with _lock:
            cals = _load_calendars()
            for c in cals:
                if c["id"] == cal_id:
                    if body.name is not None:
                        c["name"] = body.name
                    if body.color is not None:
                        c["color"] = body.color
                    if body.description is not None:
                        c["description"] = body.description
                    if body.archived is not None:
                        c["archived"] = body.archived
                    _atomic_write(CALENDARS_FILE, cals)
                    return c
        raise HTTPException(404, "Calendar not found")
    except HTTPException:
        raise
    except Exception as exc:
        log.error("update_calendar(%s): %s: %s", cal_id, type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.post("/api/calendars/{cal_id}/archive")
def archive_calendar(cal_id: str):
    """Mark a calendar as archived (hidden from default listing)."""
    return update_calendar(cal_id, CalendarUpdate(archived=True))


@app.post("/api/calendars/{cal_id}/unarchive")
def unarchive_calendar(cal_id: str):
    """Restore an archived calendar to the default listing."""
    return update_calendar(cal_id, CalendarUpdate(archived=False))


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
                # Try to fetch the proper display name from Pipeline
                # Dashboard so the calendar isn't created with the raw
                # slug (e.g. 'minecraft-server' instead of 'Minecraft
                # Server').  Falls back to the slug on any error.
                display_name = pid
                try:
                    req = urllib.request.Request(
                        f"{PIPELINE_DASHBOARD_URL}/api/projects/{pid}")
                    with _reuse_opener.open(req, timeout=2) as r:
                        pdata = json.loads(r.read())
                    display_name = pdata.get("name") or pid
                except Exception:
                    pass
                _ensure_calendar(cal_id, display_name, "project", project_id=pid)
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
        events = [e for e in events if _event_overlaps_date(e, date)]
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
    start: Optional[str] = None,
    end: Optional[str] = None,
    project_id: Optional[str] = None,
    source: Optional[str] = None,
    calendar_id: Optional[str] = None,
):
    """Cross-calendar event search.

    Query params:
      date=YYYY-MM-DD          single-day match
      from=DATE&to=DATE        inclusive range (aliases: start/end)
      start=DATE&end=DATE      inclusive range (aliases for from/to)
      project_id=X             events for any calendar tied to that project
      source=pipeline-dashboard|dream|manual
      calendar_id=X            scope to one calendar
    """
    # start/end are aliases for from/to — explicit from/to win if both given
    effective_from = from_date or start
    effective_to = to or end
    try:
        return _filter_events(
            date=date, from_date=effective_from, to=effective_to,
            project_id=project_id, source=source, calendar_id=calendar_id,
        )
    except Exception as exc:
        log.error("list_events: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/events/today")
def events_today():
    """Return events overlapping today's local date.

    Uses the canonical ``_events_overlapping_date`` helper so the count
    always agrees with the landing-page stat card and the /count endpoint.
    """
    try:
        return _events_overlapping_date(_today_iso())
    except Exception as exc:
        log.error("events_today: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable — retry")


@app.get("/api/events/today/count")
def events_today_count():
    """Lightweight endpoint returning just the today-event count.

    Clients that only need the number (e.g. dashboard badges) can hit this
    instead of fetching and counting the full event list client-side.
    Guarantees the same value as the landing page and ``/api/events/today``.
    """
    try:
        today_iso = _today_iso()
        return {"date": today_iso, "count": len(_events_overlapping_date(today_iso))}
    except Exception as exc:
        log.error("events_today_count: %s: %s", type(exc).__name__, exc)
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


@app.put("/api/events/{event_id}")
def update_event(event_id: str, body: EventUpdate):
    """Update arbitrary fields on an event."""
    try:
        with _lock:
            events = _load_events()
            for e in events:
                if e["id"] == event_id:
                    updates = body.model_dump(exclude_none=True)
                    e.update(updates)
                    e["updated_at"] = _now_iso()
                    _atomic_write(EVENTS_FILE, events)
                    return e
    except HTTPException:
        raise
    except Exception as exc:
        log.error("update_event(%s): %s: %s", event_id, type(exc).__name__, exc)
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


# ── 994deef5: timeline + orchestration APIs ────────────────────────────────

@app.get("/api/timeline")
def timeline(days: int = 7, project: Optional[str] = None):
    """Return events spanning the next ``days`` days, optionally filtered
    by project. Includes orchestration events. Sorted by start ascending.
    """
    try:
        events = _load_events()
    except Exception as exc:
        log.error("timeline: %s: %s", type(exc).__name__, exc)
        raise HTTPException(503, "Storage temporarily unavailable -- retry")
    if days < 0:
        days = 0
    today = date.today()
    horizon = today + timedelta(days=days)
    out: list[dict] = []
    for ev in events:
        start = ev.get("start") or ""
        if not start:
            continue
        try:
            ev_date = date.fromisoformat(start[:10])
        except ValueError:
            continue
        if ev_date < today or ev_date > horizon:
            continue
        if project and ev.get("project_id") != project:
            continue
        out.append(ev)
    out.sort(key=lambda e: (str(e.get("start") or ""), str(e.get("id") or "")))
    return out


@app.post("/api/orchestrations/{event_id}/run-now")
def orchestration_run_now(event_id: str):
    """Manual trigger for an orchestration event. Respects in-flight
    idempotency: returns 409 if a run is already in progress. Otherwise
    fires synchronously and returns the run record."""
    import scheduler as sched
    events = _load_events()
    event = next((e for e in events if e.get("id") == event_id), None)
    if event is None:
        raise HTTPException(404, f"event {event_id!r} not found")
    orch = sched.event_orchestration(event)
    if orch is None:
        raise HTTPException(
            400, f"event {event_id!r} has no orchestration block; not triggerable")
    if sched._in_flight(event_id):
        raise HTTPException(
            409, f"orchestration {event_id!r} already in-flight")
    record = sched.fire(orch, event_id)
    return record.to_dict()


@app.get("/api/orchestrations/{event_id}/runs")
def orchestration_runs(event_id: str, limit: int = 50):
    """Return the most-recent ``limit`` runs for the given event,
    newest-first."""
    import scheduler as sched
    runs = sched.runs_for_event(event_id)
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[:max(0, limit)]


@app.get("/api/orchestrations/status")
def orchestrations_status(limit: int = 5):
    """Operator visibility endpoint (70d0bb90). Returns every
    orchestration-enabled event with its config + last ``limit`` runs.
    Same structured payload as ``scripts/show_orchestrations.py --json``
    so PD/Dream UIs can consume one stable shape."""
    # Reuse the CLI's collector for a single source of truth.
    from pathlib import Path as _Path
    import sys as _sys
    _scripts = _Path(__file__).resolve().parent / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))
    import show_orchestrations as _sho
    return _sho.collect(_sho.DEFAULT_EVENTS, _sho.DEFAULT_RUNS_DIR,
                        limit=max(1, min(int(limit), 50)))


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
