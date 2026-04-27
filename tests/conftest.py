"""Shared fixtures for Dream Calendar tests.

The ``_isolate_storage`` autouse fixture redirects EVENTS_FILE,
CALENDARS_FILE, and EVENTS_ARCHIVE to a per-test temp directory so that
running the test suite never touches the live ``data/events.json``.

This was added to fix PD task where 752+ test-fixture events (titles like
'Task 2', 'Linked todo', 'Session N') leaked into the live store and
polluted standup velocity numbers.
"""
import pytest

import app as cal_app


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Redirect all calendar file I/O to a per-test temp directory.

    Every test gets a clean, empty store so:
    - tests never pollute the live ``data/events.json``
    - tests are fully isolated from each other
    - no manual cleanup is needed after test failures
    """
    monkeypatch.setattr(cal_app, "EVENTS_FILE", tmp_path / "events.json")
    monkeypatch.setattr(cal_app, "CALENDARS_FILE", tmp_path / "calendars.json")
    monkeypatch.setattr(cal_app, "EVENTS_ARCHIVE", tmp_path / "events_archive.jsonl")

    # Seed the personal calendar so endpoints that validate
    # _calendar_exists("personal") work in integration tests.
    cal_app._save_calendars([{
        "id": "personal",
        "name": "Personal",
        "type": "personal",
        "project_id": None,
        "color": "#3b82f6",
        "created_at": "2026-01-01T00:00:00+00:00",
    }])
