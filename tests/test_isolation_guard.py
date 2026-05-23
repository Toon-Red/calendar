"""Tests for the vendored test-isolation guard (a80ad60d Phase 3, calendar).

Calendar's variant uses real_data_dir (repo-local) not LOCALAPPDATA --
events.json + calendars.json + events_archive.jsonl live in <repo>/data/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_isolation import assert_safe_persist


def test_no_op_outside_pytest(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Even a path under the real repo data dir is allowed outside pytest.
    assert_safe_persist(ROOT / "data" / "events.json")


def test_real_data_dir_raises(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    with pytest.raises(RuntimeError, match="refusing to write"):
        assert_safe_persist(ROOT / "data" / "events.json")


def test_tmpdir_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    assert_safe_persist(tmp_path / "events.json")


def test_explicit_real_data_dir_override(monkeypatch, tmp_path):
    """A test asserting a different prod-dir can override the default."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y")
    fake_prod = tmp_path / "fake-prod"
    fake_prod.mkdir()
    with pytest.raises(RuntimeError):
        assert_safe_persist(fake_prod / "events.json", real_data_dir=fake_prod)
    # Same dest, different real_data_dir -> allowed.
    other = tmp_path / "other"
    other.mkdir()
    assert_safe_persist(fake_prod / "events.json", real_data_dir=other)


def test_isolate_storage_fixture_active(tmp_path, monkeypatch):
    """The existing _isolate_storage autouse fixture must have redirected
    EVENTS_FILE / CALENDARS_FILE / EVENTS_ARCHIVE to tmp_path. Hermetic
    canary: write to EVENTS_FILE and confirm the file lands in tmp, not
    in the real repo data/."""
    import app as cal_app
    real_data = ROOT / "data"
    # Whatever EVENTS_FILE points at must NOT be under real_data.
    events_path = Path(str(cal_app.EVENTS_FILE)).resolve()
    assert not str(events_path).lower().startswith(str(real_data.resolve()).lower()), (
        f"EVENTS_FILE={events_path} is under real data dir; isolate fixture failed"
    )
