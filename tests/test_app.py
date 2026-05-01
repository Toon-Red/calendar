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


def test_event_create_read_delete():
    """Full CRUD cycle: create → read → delete → confirm gone.

    Regression for the integration test 'Calendar: create+read+delete event'
    which hit intermittent 500s due to file-locking races on Windows.
    The delete now returns 503 (not 500) on transient I/O contention and
    the retry budget has been doubled, so the window is much smaller.
    """
    import time as _time

    # Create
    resp = client.post("/api/calendars/personal/events", json={
        "title": "crud probe",
        "start": "2030-01-01T09:00:00",
        "end": "2030-01-01T10:00:00",
        "all_day": False,
        "category": "task",
        "status": "scheduled",
        "source": "dream",
    })
    assert resp.status_code == 201, f"create returned {resp.status_code}: {resp.text}"
    event = resp.json()
    eid = event["id"]
    assert eid, "create response missing id"

    try:
        # Read
        resp = client.get(f"/api/events/{eid}")
        assert resp.status_code == 200, f"read returned {resp.status_code}"
        assert resp.json()["title"] == "crud probe"
    finally:
        # Delete (always clean up — retry once on 503 transient contention)
        resp = client.delete(f"/api/events/{eid}")
        if resp.status_code == 503:
            _time.sleep(0.2)
            resp = client.delete(f"/api/events/{eid}")
        assert resp.status_code in (200, 204), \
            f"cleanup delete returned {resp.status_code} (probe event {eid} may leak)"

    # Confirm gone
    resp = client.get(f"/api/events/{eid}")
    assert resp.status_code == 404


def test_delete_returns_503_on_io_contention(monkeypatch):
    """When the storage layer exhausts all retries on PermissionError,
    the endpoint must return 503 (not 500) so callers can distinguish
    transient I/O contention from a real server bug."""
    import app as cal_app

    fake_events = [
        {"id": "doomed", "title": "Doomed", "start": "2030-01-01T09:00:00",
         "calendar_id": "personal", "status": "scheduled"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    def _boom(*_a, **_kw):
        raise PermissionError("simulated Windows file lock")

    monkeypatch.setattr(cal_app, "_atomic_write", _boom)

    resp = client.delete("/api/events/doomed")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"


def test_create_returns_503_on_json_decode_error(monkeypatch):
    """Regression: if _load_events() hits a JSONDecodeError (e.g. reading the
    file during an atomic swap on Windows), the endpoint must return 503
    (not 500).  Previously the except clause only caught PermissionError,
    so any other exception surfaced as an opaque 500 — which is what the
    Dream EOD integration test 'Calendar: create+read+delete event' hit."""
    import app as cal_app

    def _bad_load():
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(cal_app, "_load_events", _bad_load)

    resp = client.post("/api/calendars/personal/events", json={
        "title": "probe", "start": "2030-01-01T09:00:00",
    })
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"


def test_create_returns_503_on_calendar_load_failure(monkeypatch):
    """If _load_calendars() fails (called by _calendar_exists before the
    event write), the endpoint must return 503 — not an unhandled 500."""
    import app as cal_app

    def _bad_cal_load():
        raise OSError("simulated disk error")

    monkeypatch.setattr(cal_app, "_load_calendars", _bad_cal_load)

    resp = client.post("/api/calendars/personal/events", json={
        "title": "probe", "start": "2030-01-01T09:00:00",
    })
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"


def test_load_events_graceful_on_corrupt_json(tmp_path):
    """_load_events() should return [] (not crash) when the file contains
    corrupted JSON.  This covers the scenario where a reader sees a
    truncated file during an atomic swap on Windows."""
    import app as cal_app

    corrupt_file = tmp_path / "events_corrupt.json"
    corrupt_file.write_text("{truncated", encoding="utf-8")

    original = cal_app.EVENTS_FILE
    cal_app.EVENTS_FILE = corrupt_file
    try:
        result = cal_app._load_events()
        assert result == [], f"expected [] for corrupt file, got {result!r}"
    finally:
        cal_app.EVENTS_FILE = original


def test_load_events_handles_empty_file(tmp_path):
    """An empty events file (e.g. from a crash mid-replace) should return []
    rather than raising JSONDecodeError."""
    import app as cal_app

    empty_file = tmp_path / "events_empty.json"
    empty_file.write_text("", encoding="utf-8")

    original = cal_app.EVENTS_FILE
    cal_app.EVENTS_FILE = empty_file
    try:
        result = cal_app._load_events()
        assert result == [], f"expected [] for empty file, got {result!r}"
    finally:
        cal_app.EVENTS_FILE = original


def test_get_event_not_found():
    resp = client.get("/api/events/nonexistent-id")
    assert resp.status_code == 404


# ── Pruning (PD task 08d65305) ──────────────────────────────────────────────

# ── Timeline (per-project velocity view) ───────────────────────────────────


def test_timeline_returns_by_week_structure(monkeypatch):
    """GET /api/calendars/{cal_id}/timeline returns {by_week: [...]} with the
    correct per-week shape: week_start, scheduled, completed, cancelled, events."""
    from datetime import date, timedelta
    import app as cal_app

    today = date.today()
    mon = today - timedelta(days=today.weekday())  # Monday of this week
    fake_events = [
        {"id": "a", "title": "Task A", "start": today.isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
        {"id": "b", "title": "Task B", "start": today.isoformat(),
         "status": "completed", "calendar_id": "project-dream"},
        {"id": "c", "title": "Task C", "start": today.isoformat(),
         "status": "cancelled", "calendar_id": "project-dream"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))
    monkeypatch.setattr(cal_app, "_calendar_exists", lambda cid: cid == "project-dream")

    resp = client.get("/api/calendars/project-dream/timeline?days=14")
    assert resp.status_code == 200
    body = resp.json()
    assert "by_week" in body
    assert len(body["by_week"]) >= 1

    week = body["by_week"][0]
    assert week["week_start"] == mon.isoformat()
    assert week["scheduled"] == 1
    assert week["completed"] == 1
    assert week["cancelled"] == 1
    assert len(week["events"]) == 3


def test_timeline_groups_across_multiple_weeks(monkeypatch):
    """Events spanning two different ISO weeks should land in separate buckets."""
    from datetime import date, timedelta
    import app as cal_app

    today = date.today()
    next_mon = today + timedelta(days=(7 - today.weekday()))  # Next Monday
    fake_events = [
        {"id": "w1", "title": "This week", "start": today.isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
        {"id": "w2", "title": "Next week", "start": next_mon.isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))
    monkeypatch.setattr(cal_app, "_calendar_exists", lambda cid: cid == "project-dream")

    resp = client.get("/api/calendars/project-dream/timeline?days=14")
    assert resp.status_code == 200
    weeks = resp.json()["by_week"]
    assert len(weeks) == 2
    ids_per_week = [[e["id"] for e in w["events"]] for w in weeks]
    assert ["w1"] in ids_per_week
    assert ["w2"] in ids_per_week


def test_timeline_empty_when_no_events(monkeypatch):
    """An empty calendar should return by_week=[]."""
    import app as cal_app
    monkeypatch.setattr(cal_app, "_load_events", lambda: [])
    monkeypatch.setattr(cal_app, "_calendar_exists", lambda cid: cid == "project-dream")

    resp = client.get("/api/calendars/project-dream/timeline")
    assert resp.status_code == 200
    assert resp.json() == {"by_week": []}


def test_timeline_404_for_missing_calendar():
    """Timeline on a nonexistent calendar should 404."""
    resp = client.get("/api/calendars/nonexistent-cal/timeline")
    assert resp.status_code == 404


def test_timeline_excludes_other_calendars(monkeypatch):
    """Events on a different calendar must not appear in the timeline."""
    from datetime import date
    import app as cal_app

    today = date.today()
    fake_events = [
        {"id": "mine", "title": "Mine", "start": today.isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
        {"id": "other", "title": "Other cal", "start": today.isoformat(),
         "status": "scheduled", "calendar_id": "project-other"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))
    monkeypatch.setattr(cal_app, "_calendar_exists", lambda cid: True)

    resp = client.get("/api/calendars/project-dream/timeline?days=7")
    assert resp.status_code == 200
    all_ids = [e["id"] for w in resp.json()["by_week"] for e in w["events"]]
    assert "mine" in all_ids
    assert "other" not in all_ids


def test_timeline_respects_days_param(monkeypatch):
    """Events beyond the days window must be excluded."""
    from datetime import date, timedelta
    import app as cal_app

    today = date.today()
    fake_events = [
        {"id": "near", "title": "Soon", "start": (today + timedelta(days=2)).isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
        {"id": "far", "title": "Far away", "start": (today + timedelta(days=30)).isoformat(),
         "status": "scheduled", "calendar_id": "project-dream"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))
    monkeypatch.setattr(cal_app, "_calendar_exists", lambda cid: cid == "project-dream")

    resp = client.get("/api/calendars/project-dream/timeline?days=7")
    assert resp.status_code == 200
    all_ids = [e["id"] for w in resp.json()["by_week"] for e in w["events"]]
    assert "near" in all_ids
    assert "far" not in all_ids


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
    # EVENTS_ARCHIVE already redirected to tmp_path by conftest._isolate_storage;
    # use that path for assertions.
    archive_path = cal_app.EVENTS_ARCHIVE

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


def test_prune_is_noop_when_no_events_qualify(monkeypatch):
    """Running prune twice must be a no-op the second time."""
    import app as cal_app
    monkeypatch.setattr(cal_app, "_load_events", lambda: [
        {"id": "a", "status": "scheduled", "start": "2099-01-01T09:00:00"},
    ])
    save_calls: list = []
    monkeypatch.setattr(cal_app, "_save_events",
                         lambda kept: save_calls.append(list(kept)))
    # EVENTS_ARCHIVE already redirected by conftest._isolate_storage
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


# ── Test isolation ────────────────────────────────────────────────────────────

def test_storage_isolation_no_writes_to_live_store():
    """Verify the conftest._isolate_storage fixture is working: POSTing an
    event via the TestClient must NOT create entries in the live
    data/events.json.  This is the regression guard for the 752-event
    pollution incident (PD task)."""
    from pathlib import Path
    import json
    import app as cal_app

    live_events_file = Path(__file__).resolve().parent.parent / "data" / "events.json"
    before = set()
    if live_events_file.exists():
        before = {e["id"] for e in json.loads(live_events_file.read_text(encoding="utf-8"))}

    # Write an event through the TestClient — should hit temp storage only.
    resp = client.post("/api/calendars/personal/events", json={
        "title": "isolation-probe",
        "start": "2099-12-31T00:00:00",
        "source": "dream",
    })
    assert resp.status_code == 201
    probe_id = resp.json()["id"]

    # The probe must NOT appear in the live file.
    after = set()
    if live_events_file.exists():
        after = {e["id"] for e in json.loads(live_events_file.read_text(encoding="utf-8"))}
    assert probe_id not in after, (
        f"Event {probe_id} leaked into live events.json — "
        "conftest._isolate_storage fixture is not working"
    )

    # Confirm the probe IS visible through the app (temp storage).
    resp = client.get(f"/api/events/{probe_id}")
    assert resp.status_code == 200

    # Confirm the live file wasn't modified at all.
    assert before == after, "Live events.json was modified during test"
