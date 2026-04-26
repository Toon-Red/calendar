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
import json
import logging
import os
import tempfile
import threading
import urllib.request
import uuid
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

app = FastAPI(title="Dream Calendar", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_lock = threading.Lock()


# ── Storage ──────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def _load_calendars() -> list[dict]:
    if not CALENDARS_FILE.exists():
        return []
    return json.loads(CALENDARS_FILE.read_text(encoding="utf-8"))


def _save_calendars(cals: list[dict]) -> None:
    with _lock:
        _atomic_write(CALENDARS_FILE, cals)


def _load_events() -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))


def _save_events(events: list[dict]) -> None:
    with _lock:
        _atomic_write(EVENTS_FILE, events)


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

    # Project calendars from Pipeline Dashboard (best effort)
    try:
        with urllib.request.urlopen(f"{PIPELINE_DASHBOARD_URL}/api/projects", timeout=3) as r:
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


@app.on_event("startup")
def _on_startup():
    _bootstrap()


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
    return _load_calendars()


@app.post("/api/calendars", status_code=201)
def create_calendar(body: CalendarCreate):
    cal_id = f"custom-{uuid.uuid4().hex[:8]}"
    return _ensure_calendar(cal_id, body.name, "custom", color=body.color)


@app.get("/api/calendars/{cal_id}")
def get_calendar(cal_id: str):
    for c in _load_calendars():
        if c["id"] == cal_id:
            return c
    raise HTTPException(404, "Calendar not found")


@app.delete("/api/calendars/{cal_id}")
def delete_calendar(cal_id: str):
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


@app.get("/api/calendars/{cal_id}/events")
def calendar_events(cal_id: str, from_date: Optional[str] = None, to_date: Optional[str] = None):
    if not _calendar_exists(cal_id):
        raise HTTPException(404, "Calendar not found")
    events = [e for e in _load_events() if e.get("calendar_id") == cal_id]
    if from_date:
        events = [e for e in events if _date_only(e["start"]) >= from_date]
    if to_date:
        events = [e for e in events if _date_only(e["start"]) <= to_date]
    events.sort(key=lambda e: _normalize_event_date(e["start"]))
    return events


@app.post("/api/calendars/{cal_id}/events", status_code=201)
def create_calendar_event(cal_id: str, body: EventCreate):
    """Create an event on the given calendar.

    For external sources (source != 'manual'), this is upsert-by-(source, source_id):
    if an event already exists with the same source+source_id, it's updated in place.
    The calendar is auto-created if it follows the 'project-{id}' pattern.
    """
    if not _calendar_exists(cal_id):
        if cal_id.startswith("project-"):
            pid = cal_id.removeprefix("project-")
            _ensure_calendar(cal_id, pid, "project", project_id=pid)
        else:
            raise HTTPException(404, "Calendar not found")

    payload = body.model_dump()
    payload["start"] = payload.get("start") or _today_iso()

    # Hold the lock across read+modify+write so concurrent POSTs from Dream's
    # fan-out (goal+deliverable+todo all writing at once) don't lose writes.
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


# ── Routes: events (cross-calendar) ──────────────────────────────────────────

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


@app.get("/api/events/today")
def events_today():
    return list_events(date=_today_iso())


@app.get("/api/events/{event_id}")
def get_event(event_id: str):
    for e in _load_events():
        if e["id"] == event_id:
            return e
    raise HTTPException(404, "Event not found")


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    events = _load_events()
    remaining = [e for e in events if e["id"] != event_id]
    if len(remaining) == len(events):
        raise HTTPException(404, "Event not found")
    _save_events(remaining)
    return {"ok": True}


@app.post("/api/events/{event_id}/complete")
def complete_event(event_id: str):
    events = _load_events()
    for e in events:
        if e["id"] == event_id:
            e["status"] = "completed"
            e["completed_at"] = _now_iso()
            e["updated_at"] = _now_iso()
            _save_events(events)
            return e
    raise HTTPException(404, "Event not found")


@app.post("/api/events/{event_id}/cancel")
def cancel_event(event_id: str):
    events = _load_events()
    for e in events:
        if e["id"] == event_id:
            e["status"] = "cancelled"
            e["updated_at"] = _now_iso()
            _save_events(events)
            return e
    raise HTTPException(404, "Event not found")


# ── Legacy compatibility ─────────────────────────────────────────────────────

@app.get("/api/upcoming")
def upcoming_events(hours: int = 24, trigger_automation: Optional[bool] = None):
    """Events starting within the next N hours — used by Automation."""
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


@app.get("/api/projects/{project_id}/events")
def project_events(project_id: str, from_date: Optional[str] = None, to_date: Optional[str] = None):
    return list_events(project_id=project_id, from_date=from_date, to=to_date)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5041)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
