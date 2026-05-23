"""Tests for the calendar orchestration scheduler (994deef5)."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler as sched  # noqa: E402


def _event(eid="ev1", *, start_time="08:00", catch_up=False,
           max_runs=1, kind="pd_eod", command="echo hi") -> dict:
    return {
        "id": eid, "title": f"event {eid}", "start": "2026-05-23",
        "orchestration": {
            "kind": kind, "target": "x", "command": command,
            "start_time": start_time, "catch_up": catch_up,
            "max_runs_per_day": max_runs,
        },
    }


def _fake_proc(exit_code=0, stdout="ok", stderr=""):
    return subprocess.CompletedProcess(
        args="x", returncode=exit_code, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# should_fire: timing logic
# ---------------------------------------------------------------------------

def test_does_not_fire_when_before_start_time(tmp_path):
    ev = _event(start_time="08:00", catch_up=True)
    now = datetime.now().replace(hour=7, minute=59, second=0)
    fire, reason = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is False
    assert "before start_time" in reason


def test_fires_at_exact_start_time(tmp_path):
    ev = _event(start_time="08:00", catch_up=False)
    now = datetime.now().replace(hour=8, minute=0, second=30)
    fire, _ = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is True


def test_catch_up_fires_when_past_start_time_no_run(tmp_path):
    ev = _event(start_time="08:00", catch_up=True)
    now = datetime.now().replace(hour=14, minute=0)
    fire, reason = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is True
    assert "catch-up" in reason


def test_no_catch_up_skips_when_past_fire_window(tmp_path):
    ev = _event(start_time="08:00", catch_up=False)
    now = datetime.now().replace(hour=14, minute=0)
    fire, reason = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is False
    assert "catch_up=False" in reason


def test_does_not_fire_when_completed_run_today(tmp_path):
    ev = _event(start_time="08:00", catch_up=True)
    # Persist a completed run today.
    sched._persist(sched.RunRecord(
        event_id=ev["id"],
        started_at=date.today().isoformat() + "T08:01:00+00:00",
        ended_at=date.today().isoformat() + "T08:02:00+00:00",
        exit_code=0, status="success", kind="pd_eod",
    ), base=tmp_path)
    now = datetime.now().replace(hour=14, minute=0)
    fire, reason = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is False
    assert "max_runs_per_day" in reason


def test_no_start_time_means_no_scheduled_fire(tmp_path):
    orch = {"kind": "x", "command": "echo hi"}
    fire, reason = sched.should_fire(orch, "ev1", base=tmp_path)
    assert fire is False
    assert "no start_time" in reason


def test_in_flight_skips_second_fire(tmp_path):
    ev = _event(start_time="08:00", catch_up=True)
    # Persist a running record (no ended_at).
    sched._persist(sched.RunRecord(
        event_id=ev["id"], started_at=sched._now_iso(), status="running",
    ), base=tmp_path)
    now = datetime.now().replace(hour=14, minute=0)
    fire, reason = sched.should_fire(
        ev["orchestration"], ev["id"], now=now, base=tmp_path)
    assert fire is False
    assert reason == "in-flight"


# ---------------------------------------------------------------------------
# fire: subprocess + run records + failure notification
# ---------------------------------------------------------------------------

def test_fire_records_success(tmp_path):
    ev = _event(command="echo success")
    rec = sched.fire(
        ev["orchestration"], ev["id"], base=tmp_path,
        subprocess_runner=lambda _cmd: _fake_proc(0, "success\n"),
    )
    assert rec.status == "success"
    assert rec.exit_code == 0
    runs = sched.runs_for_event(ev["id"], base=tmp_path)
    assert any(r["status"] == "success" for r in runs)


def test_fire_records_failure_and_notifies(tmp_path):
    ev = _event(command="false")
    notify = MagicMock()
    rec = sched.fire(
        ev["orchestration"], ev["id"], base=tmp_path,
        subprocess_runner=lambda _cmd: _fake_proc(1, "", "boom"),
        notify=notify,
    )
    assert rec.status == "failure"
    assert rec.exit_code == 1
    assert "boom" in rec.stderr_tail
    notify.assert_called_once()
    record_arg = notify.call_args.args[0]
    assert record_arg["event_id"] == ev["id"]
    assert record_arg["exit_code"] == 1


def test_fire_handles_runner_exception(tmp_path):
    ev = _event(command="anything")
    rec = sched.fire(
        ev["orchestration"], ev["id"], base=tmp_path,
        subprocess_runner=lambda _cmd: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="x", timeout=1)),
        notify=lambda _r: None,
    )
    assert rec.status == "failure"
    assert "TimeoutExpired" in rec.stderr_tail


# ---------------------------------------------------------------------------
# tick: orchestrates should_fire + fire for every event
# ---------------------------------------------------------------------------

def test_tick_fires_only_ready_events(tmp_path):
    runner = MagicMock(return_value=_fake_proc(0, "ok"))
    events = [
        _event("a", start_time="08:00", catch_up=True),     # fires
        _event("b", start_time="23:00", catch_up=False),    # doesn't fire (future)
        {"id": "c", "title": "no orch", "start": "2026-05-23"},  # no orch block
    ]
    now = datetime.now().replace(hour=14, minute=0)
    fired = sched.tick(
        events, now=now, base=tmp_path,
        subprocess_runner=runner,
        notify=lambda _r: None,
    )
    assert {r.event_id for r in fired} == {"a"}
    assert runner.call_count == 1


def test_tick_idempotent_when_run_already_today(tmp_path):
    ev = _event("a", start_time="08:00", catch_up=True)
    # First tick fires.
    runner = MagicMock(return_value=_fake_proc(0, "ok"))
    now = datetime.now().replace(hour=14, minute=0)
    sched.tick([ev], now=now, base=tmp_path,
               subprocess_runner=runner, notify=lambda _r: None)
    assert runner.call_count == 1
    # Second tick same day -- no fire (max_runs_per_day default = 1).
    sched.tick([ev], now=now, base=tmp_path,
               subprocess_runner=runner, notify=lambda _r: None)
    assert runner.call_count == 1


def test_event_orchestration_rejects_incomplete_block():
    assert sched.event_orchestration({"id": "x"}) is None
    assert sched.event_orchestration({"id": "x", "orchestration": "bad"}) is None
    assert sched.event_orchestration(
        {"id": "x", "orchestration": {"kind": ""}}) is None
    assert sched.event_orchestration(
        {"id": "x", "orchestration": {"kind": "k", "command": ""}}) is None
    assert sched.event_orchestration(
        {"id": "x", "orchestration": {"kind": "k", "command": "c"}}) is not None
