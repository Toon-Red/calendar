"""Lint: every event with an `orchestration.command` that invokes a
Python module MUST use `pythonw` (Windows windowless variant), never
`python` (console-subsystem -- pops a cmd.exe window on every fire).

70d0bb90 ORCHESTRATION-VISIBILITY1: the scheduler started firing
2026-05-25, and EOD/SOD events using `python -m ...` flashed ~12
cmd windows per tick. CREATE_NO_WINDOW on the parent doesn't reach a
shelled child; the right fix is to use the windowless interpreter.

Idempotent. Exits 0 when clean, 1 when a `python ` command is found,
prints the offenders. Run from repo root: `python scripts/check_no_python_in_orchestration_commands.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    events_path = REPO / "data" / "events.json"
    if not events_path.is_file():
        print(f"lint: no events.json at {events_path}; skipping", file=sys.stderr)
        return 0
    try:
        events = json.loads(events_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"lint: events.json parse error: {exc}", file=sys.stderr)
        return 1

    offenders: list[tuple[str, str]] = []
    for e in events:
        orch = e.get("orchestration") or {}
        cmd = (orch.get("command") or "").strip()
        # Only flag the bare `python ` invocation (with trailing space
        # so we don't false-match `pythonw `). curl + other shells are
        # fine.
        if cmd.startswith("python "):
            offenders.append((e.get("id", "<no-id>"), cmd))

    if not offenders:
        print("OK: no orchestration commands use console `python ` (use `pythonw` for windowless).")
        return 0

    print("FAIL: orchestration commands using console `python ` (will pop cmd window):",
          file=sys.stderr)
    for eid, cmd in offenders:
        print(f"  {eid}: {cmd}", file=sys.stderr)
    print(
        "Fix: edit data/events.json and change `python -m ...` to "
        "`pythonw -m ...` for these events (70d0bb90).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
