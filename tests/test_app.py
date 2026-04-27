"""Tests for Dream Calendar API."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


# ── Branding ────────────────────────────────────────────────────────────────

def test_app_title_is_dream_calendar():
    """FastAPI title should be 'Dream Calendar'."""
    assert app.title == "Dream Calendar"


def test_openapi_title():
    """The OpenAPI schema should expose the Dream Calendar title."""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "Dream Calendar"


def test_project_calendar_display_name():
    """The project-calendar entry in calendars.json should be named 'Dream Calendar'."""
    calendars_file = Path(__file__).resolve().parent.parent / "data" / "calendars.json"
    cals = json.loads(calendars_file.read_text(encoding="utf-8"))
    project_cal = next((c for c in cals if c["id"] == "project-calendar"), None)
    assert project_cal is not None, "project-calendar entry missing from calendars.json"
    assert project_cal["name"] == "Dream Calendar"


# ── Health ──────────────────────────────────────────────────────────────────

def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# ── Calendars CRUD ──────────────────────────────────────────────────────────

def test_list_calendars():
    resp = client.get("/api/calendars")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_calendar_not_found():
    resp = client.get("/api/calendars/nonexistent-id")
    assert resp.status_code == 404


# ── Events CRUD ─────────────────────────────────────────────────────────────

def test_list_events():
    resp = client.get("/api/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_events_today_via_query_param():
    """Sanity check the /api/events?date= path that internal callers used as
    a workaround for the now-fixed /api/events/today bug (PD task 73863fa1)."""
    from app import _today_iso
    resp = client.get(f"/api/events?date={_today_iso()}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_events_today_endpoint_returns_200(monkeypatch):
    """Regression: /api/events/today previously called list_events() directly,
    which bound from_date to a FastAPI Query() object instead of None and
    crashed with HTTP 500. PD task 73863fa1."""
    resp = client.get("/api/events/today")
    assert resp.status_code == 200, f"events/today returned {resp.status_code}: {resp.text!r}"
    body = resp.json()
    assert isinstance(body, list)


def test_events_today_returns_only_today(monkeypatch, tmp_path):
    """Today's endpoint should filter by today's date — items with
    other dates must not leak in."""
    from app import _today_iso
    today = _today_iso()
    fake_events = [
        {"id": "today1", "title": "Today A",  "start": f"{today}T09:00:00",
         "calendar_id": "personal"},
        {"id": "today2", "title": "Today B",  "start": f"{today}T15:30:00",
         "calendar_id": "personal"},
        {"id": "yest",   "title": "Yesterday","start": "2020-01-01T08:00:00",
         "calendar_id": "personal"},
        {"id": "fut",    "title": "Future",   "start": "2099-12-31T08:00:00",
         "calendar_id": "personal"},
    ]
    import app as cal_app
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))
    resp = client.get("/api/events/today")
    assert resp.status_code == 200
    ids = sorted(e["id"] for e in resp.json())
    assert ids == ["today1", "today2"]


def test_get_event_not_found():
    resp = client.get("/api/events/nonexistent-id")
    assert resp.status_code == 404


# ── Pruning (PD task 08d65305) ──────────────────────────────────────────────

def test_prune_archives_completed_events_older_than_keep_days(monkeypatch, tmp_path):
    """prune_old_completed_events() moves completed/cancelled events
    dated outside the keep window into events_archive.jsonl and shrinks
    the live store."""
    from datetime import date, timedelta
    import app as cal_app

    today = date(2026, 4, 27)
    old   = (today - timedelta(days=45)).isoformat()
    edge  = (today - timedelta(days=29)).isoformat()
    fut   = (today + timedelta(days=5)).isoformat()
    events = [
        {"id": "old-done",  "status": "completed", "start": f"{old}T09:00:00"},
        {"id": "old-cancel","status": "cancelled", "start": f"{old}T09:00:00"},
        {"id": "old-sched", "status": "scheduled", "start": f"{old}T09:00:00"},   # still scheduled — keep
        {"id": "edge-done", "status": "completed", "start": f"{edge}T09:00:00"},  # within window — keep
        {"id": "future",    "status": "scheduled", "start": f"{fut}T09:00:00"},
        {"id": "undated",   "status": "completed", "start": ""},                  # undated — keep
    ]

    saved = []
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(events))
    monkeypatch.setattr(cal_app, "_save_events", lambda kept: saved.append(list(kept)))
    archive_path = tmp_path / "events_archive.jsonl"
    monkeypatch.setattr(cal_app, "EVENTS_ARCHIVE", archive_path)

    out = cal_app.prune_old_completed_events(keep_days=30, today=today)

    assert out["archived"] == 2  # old-done + old-cancel
    assert out["kept"] == 4      # old-sched, edge-done, future, undated
    assert out["errors"] == 0
    kept_ids = {e["id"] for e in saved[0]}
    assert "old-done" not in kept_ids
    assert "old-cancel" not in kept_ids
    assert {"old-sched", "edge-done", "future", "undated"} <= kept_ids

    # archive file exists with the two pruned events
    archived_lines = archive_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(archived_lines) == 2
    archived_ids = {json.loads(line)["id"] for line in archived_lines}
    assert archived_ids == {"old-done", "old-cancel"}


def test_prune_is_noop_when_no_events_qualify(monkeypatch, tmp_path):
    """Running prune twice must be a no-op the second time."""
    import app as cal_app
    monkeypatch.setattr(cal_app, "_load_events", lambda: [
        {"id": "a", "status": "scheduled", "start": "2099-01-01T09:00:00"},
    ])
    save_calls: list = []
    monkeypatch.setattr(cal_app, "_save_events",
                         lambda kept: save_calls.append(list(kept)))
    monkeypatch.setattr(cal_app, "EVENTS_ARCHIVE", tmp_path / "events_archive.jsonl")
    out = cal_app.prune_old_completed_events(keep_days=30)
    assert out["archived"] == 0
    assert out["kept"] == 1
    # No-op shouldn't touch the live store either.
    assert save_calls == []


def test_prune_endpoint_returns_summary():
    """POST /api/events/prune wraps the helper and returns the summary."""
    resp = client.post("/api/events/prune?keep_days=99999")
    # keep_days far in the future means nothing qualifies — safe call.
    assert resp.status_code == 200
    body = resp.json()
    assert "kept" in body and "archived" in body
    assert body["archived"] == 0
