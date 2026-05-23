"""Test-isolation guard (a80ad60d Phase 3, calendar).

Calendar's data lives in repo-local ``data/`` (events.json, calendars.json,
events_archive.jsonl), NOT %LOCALAPPDATA%. So the PD/dream LOCALAPPDATA
pattern doesn't apply directly. This helper takes an explicit
``real_data_dir`` (defaults to the calendar repo's actual data/ path)
and refuses to write to anything under it during a pytest run.

Belt-and-braces alongside the existing tests/conftest.py
``_isolate_storage`` fixture which monkeypatches EVENTS_FILE / etc. to
tmp_path. If a future test bypasses the fixture, this guard refuses.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

# The literal calendar repo data/ dir -- THE production state location.
_CAL_REPO_DATA = Path(__file__).resolve().parent / "data"


def assert_safe_persist(
    dest: Union[str, Path, "os.PathLike[str]"],
    *,
    real_data_dir: Optional[Path] = None,
) -> None:
    """Raise RuntimeError if the destination is under the real calendar
    data dir during a pytest run.

    No-op outside pytest. ``real_data_dir`` defaults to the calendar
    repo's actual data/ path; pass a different dir if testing with a
    non-default layout.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    real = real_data_dir or _CAL_REPO_DATA
    real_str = os.path.normcase(str(real.resolve()))
    if os.path.normcase(str(Path(str(dest)).resolve())).startswith(real_str):
        raise RuntimeError(
            f"refusing to write to real calendar data dir during a pytest run "
            f"(dest={dest}). Use the _isolate_storage fixture in tests/conftest.py "
            f"so EVENTS_FILE / CALENDARS_FILE / EVENTS_ARCHIVE point at tmp_path. "
            f"Guard pattern: a80ad60d Phase 3 (mirrors PD's 2026-05-02 wipe response)."
        )
