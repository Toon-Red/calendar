"""Tests for 70d0bb90 ORCHESTRATION-VISIBILITY1:
1. `check_no_python_in_orchestration_commands.py` lint catches `python ` use.
2. `show_orchestrations.py` collector + renderer produce stable shape.
3. `GET /api/orchestrations/status` endpoint returns the same shape.
4. events.json on disk is consistent with the lint."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


# ---------------------------------------------------------------------------
# Lint script
# ---------------------------------------------------------------------------

def _run_lint(tmp_repo: Path) -> subprocess.CompletedProcess:
    """Invoke the lint with an alternate events.json under tmp_repo."""
    lint = REPO / "scripts" / "check_no_python_in_orchestration_commands.py"
    return subprocess.run(
        [sys.executable, str(lint)],
        cwd=str(tmp_repo),
        capture_output=True, text=True, timeout=30,
    )


def test_lint_passes_when_commands_use_pythonw(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "events.json").write_text(json.dumps([
        {"id": "good", "orchestration": {"command": "pythonw -m foo"}},
        {"id": "curl-ok", "orchestration": {"command": "curl -s http://x"}},
    ]), encoding="utf-8")
    # Shim: the lint computes REPO from its own location, not cwd.
    # Copy the lint into tmp_path/scripts/ so REPO resolves to tmp_path.
    (tmp_path / "scripts").mkdir()
    src = (REPO / "scripts" / "check_no_python_in_orchestration_commands.py")
    (tmp_path / "scripts" / src.name).write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / src.name)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_lint_fails_when_python_command_present(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "events.json").write_text(json.dumps([
        {"id": "popup-risk", "orchestration": {"command": "python -m orchestration.eod"}},
    ]), encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    src = (REPO / "scripts" / "check_no_python_in_orchestration_commands.py")
    (tmp_path / "scripts" / src.name).write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / src.name)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "popup-risk" in result.stderr
    assert "pythonw" in result.stderr  # remediation hint


def test_lint_against_live_events_json_is_clean():
    """Live events.json on master must satisfy the lint after the
    70d0bb90 swap. Regression: ensure we don't accidentally re-introduce
    `python -m ...` in a future edit."""
    import scripts.check_no_python_in_orchestration_commands as lint  # noqa: E402
    assert lint.main([]) == 0


# ---------------------------------------------------------------------------
# show_orchestrations CLI
# ---------------------------------------------------------------------------

import show_orchestrations as sho  # noqa: E402


def _seed_event_and_runs(tmp_path: Path) -> tuple[Path, Path]:
    events_path = tmp_path / "events.json"
    runs_dir = tmp_path / "orchestration-runs"
    events_path.write_text(json.dumps([
        {
            "id": "wiki-health-hourly",
            "title": "Wiki Health Audit",
            "start_time": None,
            "recurrence": "hourly",
            "orchestration": {
                "command": "curl -s -X POST http://localhost:5100/api/wiki/audit",
                "kind": "pd_wiki_health",
                "catch_up": False,
            },
        },
        {
            "id": "no-runs-yet",
            "title": "Never Fired",
            "orchestration": {"command": "pythonw -m noop", "kind": "noop"},
        },
        {
            "id": "plain-event",
            "title": "Calendar event, not an orchestration",
            # no orchestration block -> should be filtered out
        },
    ]), encoding="utf-8")
    (runs_dir / "wiki-health-hourly").mkdir(parents=True)
    (runs_dir / "wiki-health-hourly" / "2026-05-25T11-51-14.json").write_text(
        json.dumps({
            "event_id": "wiki-health-hourly",
            "started_at": "2026-05-25T11:51:14+00:00",
            "status": "ok",
            "exit_code": 0,
            "duration_secs": 4.2,
        }), encoding="utf-8")
    return events_path, runs_dir


def test_collect_includes_only_orchestration_events(tmp_path):
    events_path, runs_dir = _seed_event_and_runs(tmp_path)
    data = sho.collect(events_path, runs_dir, limit=5)
    ids = [e["id"] for e in data]
    assert "wiki-health-hourly" in ids
    assert "no-runs-yet" in ids
    assert "plain-event" not in ids  # filtered: no orchestration block


def test_collect_includes_run_history(tmp_path):
    events_path, runs_dir = _seed_event_and_runs(tmp_path)
    data = sho.collect(events_path, runs_dir, limit=5)
    wh = next(e for e in data if e["id"] == "wiki-health-hourly")
    assert len(wh["runs"]) == 1
    r = wh["runs"][0]
    assert r["status"] == "ok"
    assert r["exit_code"] == 0
    assert r["duration_secs"] == 4.2
    # Event with no runs shows empty list, NOT missing.
    no_runs = next(e for e in data if e["id"] == "no-runs-yet")
    assert no_runs["runs"] == []


def test_render_text_includes_status_and_command(tmp_path):
    events_path, runs_dir = _seed_event_and_runs(tmp_path)
    data = sho.collect(events_path, runs_dir, limit=5)
    out = sho.render_text(data)
    assert "wiki-health-hourly" in out
    assert "ok" in out  # status shown
    assert "exit=0" in out
    assert "curl -s" in out  # command truncated but visible
    assert "(none yet)" in out  # no-runs-yet event


def test_main_json_emits_valid_json(tmp_path, capsys):
    events_path, runs_dir = _seed_event_and_runs(tmp_path)
    rc = sho.main([
        "--events", str(events_path),
        "--runs-dir", str(runs_dir),
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert {"wiki-health-hourly", "no-runs-yet"} <= {e["id"] for e in payload}


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

def test_http_orchestrations_status_returns_same_shape_as_cli():
    """The endpoint reuses the CLI collector, so the shape must match."""
    import app as calendar_app
    client = TestClient(calendar_app.app)
    resp = client.get("/api/orchestrations/status?limit=3")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    # Compare to the collector against the same live events.json.
    cli_data = sho.collect(sho.DEFAULT_EVENTS, sho.DEFAULT_RUNS_DIR, limit=3)
    assert {e["id"] for e in payload} == {e["id"] for e in cli_data}
    # Each entry should carry the consumer-facing keys.
    for entry in payload:
        for k in ("id", "title", "command", "kind", "catch_up", "runs"):
            assert k in entry, f"missing key {k} in {entry}"
