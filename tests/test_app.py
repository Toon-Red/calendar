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


def test_health_includes_version():
    resp = client.get("/api/health")
    data = resp.json()
    assert "version" in data
    assert data["version"] == "0.2.0"


def test_health_includes_uptime():
    """Health endpoint should report started_at and uptime_seconds for diagnostics."""
    resp = client.get("/api/health")
    data = resp.json()
    assert "started_at" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert data["uptime_seconds"] >= 0


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


def test_events_start_end_filters(monkeypatch):
    """GET /api/events?start=DATE&end=DATE should filter to that inclusive range.
    This was the reported bug: start/end query params were silently ignored,
    returning the full event set instead of the requested window."""
    import app as cal_app

    fake_events = [
        {"id": "before", "title": "Before", "start": "2026-04-25T10:00:00",
         "calendar_id": "personal"},
        {"id": "day1", "title": "Day 1", "start": "2026-04-27T09:00:00",
         "calendar_id": "personal"},
        {"id": "day2", "title": "Day 2", "start": "2026-04-28T14:00:00",
         "calendar_id": "personal"},
        {"id": "after", "title": "After", "start": "2026-04-30T08:00:00",
         "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    resp = client.get("/api/events?start=2026-04-27&end=2026-04-28")
    assert resp.status_code == 200
    ids = sorted(e["id"] for e in resp.json())
    assert ids == ["day1", "day2"], f"expected only day1+day2, got {ids}"


def test_events_start_only(monkeypatch):
    """start= without end= should return all events from that date onward."""
    import app as cal_app

    fake_events = [
        {"id": "old", "title": "Old", "start": "2026-04-25",
         "calendar_id": "personal"},
        {"id": "new", "title": "New", "start": "2026-04-28",
         "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    resp = client.get("/api/events?start=2026-04-27")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()]
    assert ids == ["new"]


def test_events_end_only(monkeypatch):
    """end= without start= should return all events up to that date."""
    import app as cal_app

    fake_events = [
        {"id": "old", "title": "Old", "start": "2026-04-25",
         "calendar_id": "personal"},
        {"id": "new", "title": "New", "start": "2026-04-28",
         "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    resp = client.get("/api/events?end=2026-04-26")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()]
    assert ids == ["old"]


def test_events_from_to_takes_precedence_over_start_end(monkeypatch):
    """When both from/to and start/end are provided, from/to wins."""
    import app as cal_app

    fake_events = [
        {"id": "a", "title": "A", "start": "2026-04-25",
         "calendar_id": "personal"},
        {"id": "b", "title": "B", "start": "2026-04-27",
         "calendar_id": "personal"},
        {"id": "c", "title": "C", "start": "2026-04-29",
         "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    # from=2026-04-27 should win over start=2026-04-25
    resp = client.get("/api/events?start=2026-04-24&end=2026-04-30&from=2026-04-27&to=2026-04-28")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()]
    assert ids == ["b"], f"from/to should take precedence, got {ids}"


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


def test_load_events_raises_on_corrupt_json(tmp_path, monkeypatch):
    """_load_events() should raise OSError (not silently return []) when the
    file contains corrupted JSON and all retries are exhausted.  This ensures
    callers (e.g. get_event) can distinguish 'no events' from 'storage error'
    and return 503 instead of a misleading 404."""
    import app as cal_app

    monkeypatch.setattr(cal_app, "EVENTS_FILE", tmp_path / "events_corrupt.json")
    (tmp_path / "events_corrupt.json").write_text("{truncated", encoding="utf-8")
    # Speed up retries for the test
    monkeypatch.setattr("time.sleep", lambda _: None)

    import pytest
    with pytest.raises(OSError, match="temporarily unavailable"):
        cal_app._load_events()


def test_load_events_raises_on_empty_file(tmp_path, monkeypatch):
    """An empty events file (e.g. from a crash mid-replace) should raise
    OSError after retries, not silently return []."""
    import app as cal_app

    monkeypatch.setattr(cal_app, "EVENTS_FILE", tmp_path / "events_empty.json")
    (tmp_path / "events_empty.json").write_text("", encoding="utf-8")
    monkeypatch.setattr("time.sleep", lambda _: None)

    import pytest
    with pytest.raises(OSError, match="temporarily unavailable"):
        cal_app._load_events()


def test_get_event_not_found():
    resp = client.get("/api/events/nonexistent-id")
    assert resp.status_code == 404


def test_get_event_returns_503_on_storage_error(monkeypatch):
    """Regression: when _load_events() fails (e.g. file locked by antivirus
    or concurrent atomic swap on Windows), get_event must return 503 (not 404).
    Returning 404 on a transient storage error caused the Dream EOD integration
    test 'Calendar: create+read+delete event' to fail — the test created an
    event, then read it back and got a false 404 because the storage was
    momentarily unavailable."""
    import app as cal_app

    def _failing_load():
        raise OSError("simulated storage unavailable")

    monkeypatch.setattr(cal_app, "_load_events", _failing_load)

    resp = client.get("/api/events/some-id")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"


def test_list_events_returns_503_on_storage_error(monkeypatch):
    """list_events should return 503 when storage is unavailable."""
    import app as cal_app

    def _failing_load():
        raise OSError("simulated storage unavailable")

    monkeypatch.setattr(cal_app, "_load_events", _failing_load)

    resp = client.get("/api/events")
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"


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


# ── Today overlap logic ────────────────────────────────────────────────────

def test_event_overlaps_date_single_day():
    """An event with no end (or end == start) only matches its start date."""
    import app as cal_app
    assert cal_app._event_overlaps_date(
        {"start": "2026-05-03", "end": None}, "2026-05-03") is True
    assert cal_app._event_overlaps_date(
        {"start": "2026-05-03", "end": None}, "2026-05-04") is False
    assert cal_app._event_overlaps_date(
        {"start": "2026-05-03", "end": "2026-05-03"}, "2026-05-03") is True
    assert cal_app._event_overlaps_date(
        {"start": "2026-05-03", "end": "2026-05-03"}, "2026-05-02") is False


def test_event_overlaps_date_multi_day():
    """A multi-day event overlaps every date from start through end inclusive."""
    import app as cal_app
    event = {"start": "2026-05-01", "end": "2026-05-05"}
    assert cal_app._event_overlaps_date(event, "2026-04-30") is False
    assert cal_app._event_overlaps_date(event, "2026-05-01") is True
    assert cal_app._event_overlaps_date(event, "2026-05-03") is True
    assert cal_app._event_overlaps_date(event, "2026-05-05") is True
    assert cal_app._event_overlaps_date(event, "2026-05-06") is False


def test_event_overlaps_date_with_time_suffix():
    """Start/end values with time components still extract the date correctly."""
    import app as cal_app
    event = {"start": "2026-05-02T09:00:00+00:00", "end": "2026-05-04T17:00:00+00:00"}
    assert cal_app._event_overlaps_date(event, "2026-05-01") is False
    assert cal_app._event_overlaps_date(event, "2026-05-02") is True
    assert cal_app._event_overlaps_date(event, "2026-05-03") is True
    assert cal_app._event_overlaps_date(event, "2026-05-04") is True
    assert cal_app._event_overlaps_date(event, "2026-05-05") is False


def test_event_overlaps_date_missing_start():
    """Events without a start field never match any date."""
    import app as cal_app
    assert cal_app._event_overlaps_date({"end": "2026-05-03"}, "2026-05-03") is False
    assert cal_app._event_overlaps_date({}, "2026-05-03") is False


def test_events_today_endpoint_uses_overlap(monkeypatch):
    """/api/events/today should return events overlapping today, including
    multi-day events that started before today."""
    import app as cal_app
    from app import _today_iso
    today = _today_iso()
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    fake_events = [
        {"id": "single-today", "title": "Single day today",
         "start": today, "end": None, "calendar_id": "personal"},
        {"id": "multi-spans", "title": "Multi-day spanning today",
         "start": yesterday, "end": tomorrow, "calendar_id": "personal"},
        {"id": "ended-yesterday", "title": "Ended yesterday",
         "start": "2026-01-01", "end": yesterday, "calendar_id": "personal"},
        {"id": "starts-tomorrow", "title": "Starts tomorrow",
         "start": tomorrow, "end": None, "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    resp = client.get("/api/events/today")
    assert resp.status_code == 200
    ids = sorted(e["id"] for e in resp.json())
    assert ids == ["multi-spans", "single-today"]


def test_events_today_count_endpoint(monkeypatch):
    """/api/events/today/count returns the same number as len(/api/events/today)."""
    import app as cal_app
    from app import _today_iso
    today = _today_iso()

    fake_events = [
        {"id": "a", "title": "A", "start": today, "end": None, "calendar_id": "personal"},
        {"id": "b", "title": "B", "start": today, "end": None, "calendar_id": "personal"},
        {"id": "c", "title": "C", "start": "2020-01-01", "end": None, "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    resp_list = client.get("/api/events/today")
    resp_count = client.get("/api/events/today/count")
    assert resp_list.status_code == 200
    assert resp_count.status_code == 200
    assert resp_count.json()["count"] == len(resp_list.json())
    assert resp_count.json()["count"] == 2
    assert resp_count.json()["date"] == today


def test_landing_page_today_matches_api(monkeypatch):
    """The landing page 'Today' stat card must show the same count as
    /api/events/today — this was the original reported bug."""
    import app as cal_app
    from app import _today_iso
    today = _today_iso()
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    next_week = (date.today() + timedelta(days=7)).isoformat()

    fake_events = [
        {"id": "t1", "title": "Today", "start": today, "end": None, "calendar_id": "personal"},
        {"id": "t2", "title": "Spans", "start": yesterday, "end": next_week, "calendar_id": "personal"},
        {"id": "t3", "title": "Old", "start": "2020-01-01", "end": None, "calendar_id": "personal"},
    ]
    monkeypatch.setattr(cal_app, "_load_events", lambda: list(fake_events))

    # Landing page HTML should show count 2
    resp_html = client.get("/")
    assert resp_html.status_code == 200
    # The number appears in a <div class="num">N</div> element
    assert '>2</div>' in resp_html.text

    # API endpoint should return 2 events
    resp_api = client.get("/api/events/today")
    assert resp_api.status_code == 200
    assert len(resp_api.json()) == 2

    # Count endpoint agrees
    resp_count = client.get("/api/events/today/count")
    assert resp_count.json()["count"] == 2
