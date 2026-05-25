"""Operator visibility: what orchestrations are configured + what's run.

70d0bb90 ORCHESTRATION-VISIBILITY1: after the scheduler went live
(SCHEDULER-LIFESPAN-WIRE1 / 969f4494), the operator had no surface
for "what's scheduled + what just ran" short of grepping data/. This
CLI walks events.json + data/orchestration-runs/ and renders a compact
status table with the last 5 runs per event.

Modes:
  default            one-shot render
  --tail             refresh every 10s (Ctrl-C to exit)
  --json             machine-readable; suitable for piping
  --interval N       override --tail refresh interval (seconds)

Idempotent re-runs; never writes to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS = REPO / "data" / "events.json"
DEFAULT_RUNS_DIR = REPO / "data" / "orchestration-runs"

# ANSI escapes (truncated set; safe to print on non-tty -- terminals
# that don't support them just show the raw code which is mildly
# ugly but never wrong).
RESET = "\x1b[0m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
BOLD = "\x1b[1m"


def _color_for(status: str) -> str:
    s = (status or "").lower()
    if s == "ok":
        return GREEN
    if s == "running":
        return YELLOW
    if s in ("failed", "error", "timeout"):
        return RED
    return DIM


@dataclass
class RunSummary:
    started_at: str
    status: str
    exit_code: Optional[int]
    duration_secs: Optional[float]
    path: Path

    @classmethod
    def from_file(cls, p: Path) -> Optional["RunSummary"]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return cls(
            started_at=str(d.get("started_at", "")),
            status=str(d.get("status", "?")),
            exit_code=d.get("exit_code"),
            duration_secs=d.get("duration_secs"),
            path=p,
        )


def _load_runs(event_id: str, runs_dir: Path, limit: int = 5
               ) -> list[RunSummary]:
    edir = runs_dir / event_id
    if not edir.is_dir():
        return []
    files = sorted(edir.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        rs = RunSummary.from_file(f)
        if rs is not None:
            out.append(rs)
    return out


def collect(events_path: Path = DEFAULT_EVENTS,
            runs_dir: Path = DEFAULT_RUNS_DIR,
            limit: int = 5) -> list[dict]:
    """Return a structured list: one entry per orchestration event,
    each with its config + last N runs. Pure (no I/O beyond the
    given paths)."""
    if not events_path.is_file():
        return []
    try:
        events = json.loads(events_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for e in events:
        orch = e.get("orchestration")
        if not orch:
            continue
        runs = _load_runs(e["id"], runs_dir, limit=limit)
        out.append({
            "id": e["id"],
            "title": e.get("title", ""),
            "start_time": e.get("start_time"),
            "recurrence": e.get("recurrence"),
            "command": orch.get("command", ""),
            "kind": orch.get("kind", ""),
            "catch_up": orch.get("catch_up", False),
            "max_runs_per_day": orch.get("max_runs_per_day"),
            "runs": [
                {
                    "started_at": r.started_at,
                    "status": r.status,
                    "exit_code": r.exit_code,
                    "duration_secs": r.duration_secs,
                    "file": str(r.path.name),
                }
                for r in runs
            ],
        })
    return out


def render_text(data: list[dict]) -> str:
    if not data:
        return f"{DIM}(no orchestration events found){RESET}"
    lines: list[str] = []
    lines.append(f"{BOLD}Calendar orchestrations ({len(data)} configured){RESET}")
    lines.append("")
    for ev in data:
        cmd = ev["command"]
        cmd_disp = cmd if len(cmd) <= 80 else cmd[:77] + "..."
        recur = ev["recurrence"] or "(one-shot)"
        catch = " catch_up" if ev["catch_up"] else ""
        lines.append(
            f"{CYAN}{ev['id']}{RESET} "
            f"{DIM}({ev['kind']}){RESET}"
        )
        lines.append(f"  title: {ev['title']}")
        lines.append(f"  schedule: {recur}{catch}  start: {ev['start_time'] or '(immediate)'}")
        lines.append(f"  command: {cmd_disp}")
        if not ev["runs"]:
            lines.append(f"  {DIM}runs: (none yet){RESET}")
        else:
            lines.append(f"  recent runs (newest first):")
            for r in ev["runs"]:
                color = _color_for(r["status"])
                ts = (r["started_at"] or "?")[:19]
                xc = r["exit_code"] if r["exit_code"] is not None else "-"
                dur = (f"{r['duration_secs']:.1f}s"
                       if r["duration_secs"] is not None else "-")
                lines.append(
                    f"    {ts}  {color}{r['status']:<10}{RESET}"
                    f"  exit={xc:<4}  dur={dur}"
                )
        lines.append("")
    return "\n".join(lines)


def _clear_screen() -> None:
    # ANSI clear + cursor home; works on Windows Terminal +
    # virtually every modern terminal.
    sys.stdout.write("\x1b[2J\x1b[H")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--events", default=str(DEFAULT_EVENTS))
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--limit", type=int, default=5,
                        help="last N runs per event (default 5)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--tail", action="store_true",
                        help="refresh every --interval seconds")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="seconds between refreshes in --tail mode")
    args = parser.parse_args(argv)

    events_path = Path(args.events)
    runs_dir = Path(args.runs_dir)

    def _render_once() -> str:
        data = collect(events_path, runs_dir, limit=args.limit)
        if args.json:
            return json.dumps(data, indent=2, default=str)
        return render_text(data)

    if not args.tail:
        print(_render_once())
        return 0

    # --tail mode: clear + render, sleep interval, repeat.
    try:
        while True:
            _clear_screen()
            print(_render_once())
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()  # newline after ^C
        return 0


if __name__ == "__main__":
    sys.exit(main())
