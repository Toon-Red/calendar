"""Seed the two orchestration events for PD EOD + SOD (994deef5).

Idempotent: existing events with the seed ids are left alone (or
updated in place with the latest orchestration block). Safe to re-run.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVENTS_FILE = REPO_ROOT / "data" / "events.json"

SEEDS = [
    {
        "id": "eod-daily",
        "calendar_id": "personal",
        "title": "EOD Orchestration",
        "start": date.today().isoformat(),
        "all_day": True,
        "status": "scheduled",
        "source": "994deef5-seed",
        "category": "milestone",
        "description": (
            "Calendar-driven EOD orchestration trigger. Fires at 17:00 "
            "local with catch-up: if past 17:00 with no completed run "
            "today, fires on the next 60s tick. See scheduler.py for "
            "the fire rules."
        ),
        "recurring": "daily",
        "trigger_automation": True,
        "orchestration": {
            "kind": "pd_eod",
            "target": "pipeline-dashboard",
            "command": "python -m orchestration.eod",
            "start_time": "17:00",
            "catch_up": True,
            "max_runs_per_day": 1,
        },
    },
    {
        "id": "sod-daily",
        "calendar_id": "personal",
        "title": "Start of Day Orchestration",
        "start": date.today().isoformat(),
        "all_day": True,
        "status": "scheduled",
        "source": "994deef5-seed",
        "category": "milestone",
        "description": (
            "Calendar-driven SOD orchestration trigger. Fires at 08:00 "
            "local with catch-up. Real SOD orchestrator pending "
            "(PD-SOD1 follow-on); current target is the stub at "
            "orchestration.sod which prints + exits 0."
        ),
        "recurring": "daily",
        "trigger_automation": True,
        "orchestration": {
            "kind": "pd_sod",
            "target": "pipeline-dashboard",
            "command": "python -m orchestration.sod",
            "start_time": "08:00",
            "catch_up": True,
            "max_runs_per_day": 1,
        },
    },
]


def main() -> int:
    if not EVENTS_FILE.is_file():
        print(f"FATAL: events file not found at {EVENTS_FILE}", file=sys.stderr)
        return 2
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    by_id = {e.get("id"): e for e in events if isinstance(e, dict)}
    added = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for seed in SEEDS:
        seed_with_ts = dict(seed)
        if seed["id"] in by_id:
            by_id[seed["id"]].update(seed_with_ts)
            by_id[seed["id"]]["updated_at"] = now
            updated += 1
        else:
            seed_with_ts["created_at"] = now
            seed_with_ts["updated_at"] = now
            events.append(seed_with_ts)
            added += 1
    EVENTS_FILE.write_text(
        json.dumps(events, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Seeded orchestration events: {added} added, {updated} updated, "
          f"total events now {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
