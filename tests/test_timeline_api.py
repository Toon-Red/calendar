"""Tests for the calendar timeline + orchestration HTTP APIs (994deef5)."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as cal_app  # noqa: E402
import scheduler as sched  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Redirect orchestration-runs storage to the per-test tmp dir.
    monkeypatch.setattr(sched, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(sched, "DATA_DIR", tmp_path)
    return TestClient(cal_app.app)


def _seed_event(event_id="ev-x", *, start=None, orchestration=None,
                project_id=None) -> dict:
    events = cal_app._load_events()
    today = date.today().isoformat()
    ev = {
        "id": event_id, "calendar_id": "personal",
        "title": f"event {event_id}", "start": start or today,
        "all_day": True, "status": "scheduled", "source": "test",
        "source_id": None, "project_id": project_id, "category": "task",
        "description": None, "recurring": None, "trigger_automation": False,
        "orchestration": orchestration,
    }
    events.append(ev)
    cal_app._save_events(events)
    return ev


# ---------------------------------------------------------------------------
# GET /api/timeline
# ---------------------------------------------------------------------------

def test_timeline_returns_events_ordered_by_start(client):
    today = date.today()
    _seed_event("a", start=(today + timedelta(days=2)).isoformat())
    _seed_event("b", start=(today + timedelta(days=0)).isoformat())
    _seed_event("c", start=(today + timedelta(days=1)).isoformat())
    resp = client.get("/api/timeline?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    starts = [e["start"] for e in body]
    assert starts == sorted(starts)
    assert {e["id"] for e in body} == {"a", "b", "c"}


def test_timeline_horizon_excludes_far_future(client):
    today = date.today()
    _seed_event("near", start=today.isoformat())
    _seed_event("far", start=(today + timedelta(days=30)).isoformat())
    resp = client.get("/api/timeline?days=7")
    ids = {e["id"] for e in resp.json()}
    assert "near" in ids
    assert "far" not in ids


def test_timeline_filters_by_project(client):
    today = date.today()
    _seed_event("a", start=today.isoformat(), project_id="pdash")
    _seed_event("b", start=today.isoformat(), project_id="other")
    resp = client.get("/api/timeline?days=7&project=pdash")
    ids = {e["id"] for e in resp.json()}
    assert ids == {"a"}


def test_timeline_includes_orchestration_events(client):
    today = date.today()
    _seed_event("eod-x", start=today.isoformat(), orchestration={
        "kind": "pd_eod", "command": "echo hi",
        "start_time": "17:00", "catch_up": True,
    })
    body = client.get("/api/timeline?days=1").json()
    eod = next(e for e in body if e["id"] == "eod-x")
    assert eod["orchestration"]["kind"] == "pd_eod"


# ---------------------------------------------------------------------------
# POST /api/orchestrations/{event_id}/run-now
# ---------------------------------------------------------------------------

def test_run_now_fires_orchestration(client, monkeypatch):
    today = date.today().isoformat()
    _seed_event("rn-1", start=today, orchestration={
        "kind": "pd_eod", "command": "echo run-now",
        "start_time": "08:00", "catch_up": True,
    })
    # Stub the subprocess runner so the test doesn't actually exec.
    monkeypatch.setattr(sched, "_default_runner",
        lambda cmd: subprocess.CompletedProcess(args=cmd, returncode=0,
                                                 stdout="ran", stderr=""))
    resp = client.post("/api/orchestrations/rn-1/run-now")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["event_id"] == "rn-1"


def test_run_now_returns_409_when_in_flight(client):
    today = date.today().isoformat()
    _seed_event("rn-2", start=today, orchestration={
        "kind": "pd_eod", "command": "echo x",
        "start_time": "08:00", "catch_up": True,
    })
    # Manually persist a running record.
    sched._persist(sched.RunRecord(
        event_id="rn-2", started_at=sched._now_iso(), status="running",
    ))
    resp = client.post("/api/orchestrations/rn-2/run-now")
    assert resp.status_code == 409, resp.text
    assert "in-flight" in resp.json()["detail"]


def test_run_now_404_when_event_missing(client):
    resp = client.post("/api/orchestrations/missing-event/run-now")
    assert resp.status_code == 404


def test_run_now_400_when_event_has_no_orchestration(client):
    today = date.today().isoformat()
    _seed_event("plain", start=today, orchestration=None)
    resp = client.post("/api/orchestrations/plain/run-now")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/orchestrations/{event_id}/runs
# ---------------------------------------------------------------------------

def test_runs_endpoint_returns_history_newest_first(client):
    sched._persist(sched.RunRecord(
        event_id="hist-1",
        started_at="2026-05-21T08:00:00+00:00",
        ended_at="2026-05-21T08:01:00+00:00",
        exit_code=0, status="success", kind="pd_eod",
    ))
    sched._persist(sched.RunRecord(
        event_id="hist-1",
        started_at="2026-05-23T08:00:00+00:00",
        ended_at="2026-05-23T08:01:00+00:00",
        exit_code=0, status="success", kind="pd_eod",
    ))
    resp = client.get("/api/orchestrations/hist-1/runs")
    body = resp.json()
    assert resp.status_code == 200
    assert len(body) == 2
    assert body[0]["started_at"] > body[1]["started_at"]


def test_runs_endpoint_respects_limit(client):
    for i in range(5):
        sched._persist(sched.RunRecord(
            event_id="limit-test",
            started_at=f"2026-05-2{i}T08:00:00+00:00",
            ended_at=f"2026-05-2{i}T08:01:00+00:00",
            exit_code=0, status="success",
        ))
    body = client.get("/api/orchestrations/limit-test/runs?limit=2").json()
    assert len(body) == 2
