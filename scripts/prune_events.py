"""Prune bloated events.json: remove test fixtures, de-duplicate, archive stale events.

Reference date: 2026-05-16

Strategy:
1. Remove events from known test-fixture / deprecated calendars.
2. Remove events matching synthetic/seed title patterns.
3. Remove events with malformed timestamps.
4. De-duplicate: for any (title, calendar_id) pair with multiple entries,
   keep only the most recently created one.
5. Archive old completed/cancelled events (>30 days before reference).
6. Write atomically to prevent concurrent server writes from corrupting output.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# --- Configuration -----------------------------------------------------------

REFERENCE_DATE = date(2026, 5, 16)
ARCHIVE_CUTOFF = REFERENCE_DATE - timedelta(days=30)  # 2026-04-16

# Calendars to remove entirely (test fixtures and deprecated)
REMOVE_CALENDAR_IDS: set[str] = {
    "project-proj1",
    "project-proj-x",
    "project-proj-a",
    "project-p1",
    "project-ac",
    "project-proj2",
    "project-project-a",
    "project-project-b",
}

# Regex patterns matching synthetic/seed event titles to remove entirely
SYNTHETIC_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^Task \d+$"),                       # "Task 0", "Task 1", ...
    re.compile(r"^Session \d+$"),                    # "Session 0", "Session 1", ...
    re.compile(r"^noise-\d+$"),                      # "noise-0", "noise-1", ...
    re.compile(r"^\[Goal\] Empty Goal$"),
    re.compile(r"^\[Goal\] Empty goal$"),
    re.compile(r"^\[Goal\] Has Tasks$"),
    re.compile(r"^\[Goal\] Learn Python$"),
    re.compile(r"^\[Goal\] Goal [A-Z]$"),            # "Goal A", "Goal B"
    re.compile(r"^\[Goal\] Q\d+ Goal$"),             # "Q1 Goal"
    re.compile(r"^\[Goal\] ICS Test Goal$"),
    re.compile(r"^\[Goal\] Priority Test$"),
    re.compile(r"^\[Goal\] Progress Goal$"),
    re.compile(r"^\[Goal\] Stalled Goal$"),
    re.compile(r"^\[Goal\] Long-term Goal$"),
    re.compile(r"^\[Goal\] Urgent Goal$"),
    re.compile(r"^\[Goal\] Big Goal$"),
    re.compile(r"^\[Goal\] EOD Goal$"),
    re.compile(r"^\[Goal\] Active Goal$"),
    re.compile(r"^\[Goal\] Work Goal$"),
    re.compile(r"^\[Goal\] Cap Goal$"),
    re.compile(r"^\[Goal\] Placeholder Goal$"),
    re.compile(r"^\[Goal\] Link Goal$"),
    re.compile(r"^\[Goal\] Default Goal$"),
    re.compile(r"^\[Goal\] Read a book$"),
    re.compile(r"^\[Goal\] Personal goal$"),
    re.compile(r"^\[Goal\] Learn Rust$"),
    re.compile(r"^\[Goal\] Learn Go$"),
    re.compile(r"^\[Goal\] Ship v1\.0$"),
    re.compile(r"^\[Goal\] Ship MVP$"),
    re.compile(r"^\[Goal\] Done Goal$"),
    re.compile(r"^\[Goal\] Planned Goal$"),
    re.compile(r"^\[Goal\] Goal with todos$"),
    re.compile(r"^\[Goal\] Fully done goal$"),
    re.compile(r"^\[Goal\] Critical project$"),
    re.compile(r"^\[Goal\] Dream goal$"),
    re.compile(r"^\[Goal\] Ship it$"),
    re.compile(r"^\[Goal\] Ship Dream v1$"),
    re.compile(r"^\[Deliverable\] A task$"),
    re.compile(r"^\[Deliverable\] Read docs$"),
    re.compile(r"^\[Deliverable\] Week \d+ task$"),
    re.compile(r"^\[Deliverable\] Task$"),
    re.compile(r"^\[Deliverable\] Task [A-Z]$"),     # "Task A", "Task B"
    re.compile(r"^\[Deliverable\] Task from [A-Z]$"),  # "Task from A"
    re.compile(r"^\[Deliverable\] Completed deliverable$"),
    re.compile(r"^\[Deliverable\] Near deadline$"),
    re.compile(r"^\[Deliverable\] Far deadline$"),
    re.compile(r"^\[Deliverable\] Write report$"),
    re.compile(r"^\[Deliverable\] Read chapter \d+$"),
    re.compile(r"^\[Deliverable\] Build API$"),
    re.compile(r"^\[Deliverable\] Already done$"),
    re.compile(r"^\[Deliverable\] Active Task$"),
    re.compile(r"^\[Deliverable\] Active deliverable$"),
    re.compile(r"^\[Deliverable\] Morning work$"),
    re.compile(r"^\[Deliverable\] Big task \d+$"),
    re.compile(r"^\[Deliverable\] Standalone task$"),
    re.compile(r"^\[Deliverable\] EOD Task$"),
    re.compile(r"^\[Deliverable\] Link Task$"),
    re.compile(r"^\[Deliverable\] Do exercises$"),
    re.compile(r"^\[Deliverable\] Cap Task$"),
    re.compile(r"^\[Deliverable\] Default Task$"),
    re.compile(r"^\[Deliverable\] Parent task$"),
    re.compile(r"^\[Deliverable\] Write tests$"),
    re.compile(r"^(Code|Study|Read)$"),              # Generic single-word seeds
    re.compile(r"^Morning workout$"),
    re.compile(r"^Morning run$"),
    re.compile(r"^Morning code$"),
    re.compile(r"^Morning coding$"),
    re.compile(r"^Test Event$"),
    re.compile(r"^Done task$"),
    re.compile(r"^Manually postponed$"),
    re.compile(r"^Linked todo$"),
    re.compile(r"^Incomplete task$"),
]

ARCHIVABLE_STATUSES: set[str] = {"completed", "cancelled"}

# --- Paths -------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS_FILE = DATA_DIR / "events.json"
ARCHIVE_FILE = DATA_DIR / "events_archive.jsonl"


def parse_start_date(event: dict[str, Any]) -> date | None:
    """Extract the date portion from the 'start' field (ISO date or datetime)."""
    raw = event.get("start")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def has_malformed_time(event: dict[str, Any]) -> bool:
    """Detect timestamps with impossible hours like T100:00:00."""
    raw = event.get("start", "")
    if "T" in raw:
        time_part = raw.split("T", 1)[1]
        if re.match(r"\d{3,}:", time_part):
            return True
    return False


def is_synthetic(title: str) -> bool:
    """Check if a title matches known synthetic/seed patterns."""
    return any(pat.match(title) for pat in SYNTHETIC_TITLE_PATTERNS)


def write_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically using a temp file + rename."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from test_isolation import assert_safe_persist  # a80ad60d Phase 3
    assert_safe_persist(path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=path.stem, suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> None:
    # -------------------------------------------------------------------------
    # 1. Load events
    # -------------------------------------------------------------------------
    if not EVENTS_FILE.exists():
        print(f"ERROR: {EVENTS_FILE} not found.", file=sys.stderr)
        sys.exit(1)

    with EVENTS_FILE.open("r", encoding="utf-8") as fh:
        events: list[dict[str, Any]] = json.load(fh)

    total_loaded = len(events)
    print(f"Loaded {total_loaded} events from {EVENTS_FILE.name}")

    # -------------------------------------------------------------------------
    # 2. Remove events from bad calendars
    # -------------------------------------------------------------------------
    removed_calendars = 0
    remaining: list[dict[str, Any]] = []

    for ev in events:
        if ev.get("calendar_id") in REMOVE_CALENDAR_IDS:
            removed_calendars += 1
        else:
            remaining.append(ev)

    print(f"  Removed {removed_calendars} events from test/deprecated calendars")

    # -------------------------------------------------------------------------
    # 3. Remove synthetic seed events by title pattern
    # -------------------------------------------------------------------------
    removed_synthetic = 0
    after_synthetic: list[dict[str, Any]] = []

    for ev in remaining:
        title = ev.get("title", "")
        if is_synthetic(title):
            removed_synthetic += 1
        else:
            after_synthetic.append(ev)

    print(f"  Removed {removed_synthetic} synthetic/seed events by title pattern")

    # -------------------------------------------------------------------------
    # 4. Remove events with malformed timestamps
    # -------------------------------------------------------------------------
    removed_malformed = 0
    after_malformed: list[dict[str, Any]] = []

    for ev in after_synthetic:
        if has_malformed_time(ev):
            removed_malformed += 1
        else:
            after_malformed.append(ev)

    print(f"  Removed {removed_malformed} events with malformed timestamps")

    # -------------------------------------------------------------------------
    # 5. De-duplicate: same (title, calendar_id) -> keep only newest
    # -------------------------------------------------------------------------
    groups: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for ev in after_malformed:
        key = (ev.get("title", ""), ev.get("calendar_id", ""))
        groups[key].append(ev)

    deduped: list[dict[str, Any]] = []
    duplicates_removed = 0

    for _key, group in groups.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            # Sort by created_at descending, keep the newest
            group.sort(key=lambda e: e.get("created_at", ""), reverse=True)
            deduped.append(group[0])
            duplicates_removed += len(group) - 1

    print(f"  Removed {duplicates_removed} duplicate events (same title+calendar)")

    # -------------------------------------------------------------------------
    # 6. Archive old completed/cancelled events
    # -------------------------------------------------------------------------
    kept: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []

    for ev in deduped:
        status = ev.get("status", "").lower()
        start_dt = parse_start_date(ev)

        if (
            status in ARCHIVABLE_STATUSES
            and start_dt is not None
            and start_dt < ARCHIVE_CUTOFF
        ):
            archived.append(ev)
        else:
            kept.append(ev)

    print(f"  Archived {len(archived)} old completed/cancelled events (before {ARCHIVE_CUTOFF})")

    # Append archived events to the JSONL archive file
    with ARCHIVE_FILE.open("a", encoding="utf-8") as fh:
        for ev in archived:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"  Appended to {ARCHIVE_FILE.name}")

    # -------------------------------------------------------------------------
    # 7. Save cleaned events atomically
    # -------------------------------------------------------------------------
    kept.sort(key=lambda e: (e.get("start") or "", e.get("created_at") or ""))

    write_atomic(EVENTS_FILE, kept)

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print()
    print("=" * 50)
    print("  PRUNING SUMMARY")
    print("=" * 50)
    print(f"  Total loaded:              {total_loaded:>6}")
    print(f"  Bad calendars removed:     {removed_calendars:>6}")
    print(f"  Synthetic seeds removed:   {removed_synthetic:>6}")
    print(f"  Malformed timestamps:      {removed_malformed:>6}")
    print(f"  Duplicates collapsed:      {duplicates_removed:>6}")
    print(f"  Archived (old done):       {len(archived):>6}")
    print(f"  ----------------------------------------")
    print(f"  EVENTS KEPT:               {len(kept):>6}")
    print("=" * 50)
    print()
    print(f"  Output: {EVENTS_FILE}")
    print(f"  Archive: {ARCHIVE_FILE}")


if __name__ == "__main__":
    main()
