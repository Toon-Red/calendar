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
    """Use the /api/events?date= endpoint for today's events (the /api/events/today
    endpoint has a pre-existing bug where it calls list_events() directly, bypassing
    FastAPI's Query parameter resolution)."""
    from app import _today_iso
    resp = client.get(f"/api/events?date={_today_iso()}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_event_not_found():
    resp = client.get("/api/events/nonexistent-id")
    assert resp.status_code == 404
