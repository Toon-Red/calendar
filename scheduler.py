"""Calendar-driven orchestration scheduler (994deef5).

Calendar events with an ``orchestration`` field are scheduled jobs.
The tick loop in this module reads events on a 60s cadence, decides
which orchestrations should fire NOW (start_time + catch_up +
max_runs_per_day rules), and dispatches via subprocess. Run records
land in ``data/orchestration-runs/<event_id>/<ts>.json``.

This module owns the WHEN; the calendar service owns event storage;
the dispatched commands own the WHAT. Three concerns, three layers.

Orchestration schema (lives on the calendar event):
    {
        "kind": "pd_eod",                              # category label, free text
        "target": "pipeline-dashboard",                # project_id this serves
        "command": "python -m pipeline_dashboard...",  # subprocess invocation
        "catch_up": true,                              # fire today if past start_time
                                                        # AND no completed run
        "max_runs_per_day": 1
    }

Fire rules:
  * If the event has no ``start_time``, never fires (scheduling is
    explicit; absence of a time means the event is informational).
  * On each tick, for each orchestration event:
      - If a run is in-flight for this event id, skip.
      - If today's completed-run count >= max_runs_per_day, skip.
      - If current local time >= start_time AND no completed run
        today: FIRE.
      - If catch_up is False, ALSO requires current time within
        the same minute as start_time (60s tolerance).

Failure handling:
  * Subprocess exit != 0 records the run with exit_code + stderr
    snippet and notifies via ``notify_failure(record)`` (overridable
    for tests; the default Discord push is a no-op when no webhook
    is configured). NO automatic retry.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

log = logging.getLogger("calendar.scheduler")

DATA_DIR = Path(__file__).parent / "data"
RUNS_DIR = DATA_DIR / "orchestration-runs"
TICK_INTERVAL_SECONDS = 60


@dataclass
class RunRecord:
    event_id: str
    started_at: str
    ended_at: Optional[str] = None
    exit_code: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    status: str = "running"  # running | success | failure | skipped
    skip_reason: Optional[str] = None
    kind: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "kind": self.kind,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


def _local_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def _parse_hhmm(value: Any) -> Optional[tuple[int, int]]:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def event_orchestration(event: Mapping[str, Any]) -> Optional[dict]:
    """Return the orchestration dict if the event has one, else None.

    Defensive against partial / typo-only orchestration blocks: requires
    at minimum ``kind`` and ``command`` to count as a real trigger.
    """
    block = event.get("orchestration") if isinstance(event, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    kind = block.get("kind")
    command = block.get("command")
    if not isinstance(kind, str) or not kind.strip():
        return None
    if not isinstance(command, str) or not command.strip():
        return None
    return dict(block)


def _runs_dir_for(event_id: str, base: Optional[Path] = None) -> Path:
    root = base if base is not None else RUNS_DIR
    return Path(root) / event_id


def _list_run_files(event_id: str, base: Optional[Path] = None) -> list[Path]:
    d = _runs_dir_for(event_id, base)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def runs_for_event(event_id: str, base: Optional[Path] = None) -> list[dict]:
    """Read all run records for an event in chronological order."""
    out: list[dict] = []
    for p in _list_run_files(event_id, base):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _completed_runs_today(event_id: str, base: Optional[Path] = None) -> int:
    today = _today_iso()
    n = 0
    for rec in runs_for_event(event_id, base):
        if rec.get("status") in ("success", "failure") and \
                rec.get("started_at", "").startswith(today):
            n += 1
    return n


def _in_flight(event_id: str, base: Optional[Path] = None) -> bool:
    for rec in runs_for_event(event_id, base):
        if rec.get("status") == "running":
            return True
    return False


def _persist(record: RunRecord, base: Optional[Path] = None) -> Path:
    d = _runs_dir_for(record.event_id, base)
    d.mkdir(parents=True, exist_ok=True)
    safe = record.started_at.replace(":", "-").replace("+", "_")
    path = d / f"{safe}.json"
    path.write_text(
        json.dumps(record.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def notify_failure_default(record: dict) -> None:
    """Discord ping hook. No-op when DREAM_DISCORD_WEBHOOK is unset.
    Pure best-effort; never raises."""
    webhook = os.environ.get("DREAM_DISCORD_WEBHOOK")
    if not webhook:
        log.warning("orchestration %s failed (exit=%s, kind=%s); no webhook configured",
                    record.get("event_id"), record.get("exit_code"),
                    record.get("kind"))
        return
    try:
        import urllib.request
        body = json.dumps({
            "content": (
                f":warning: orchestration `{record.get('kind')}` "
                f"(event `{record.get('event_id')}`) failed with "
                f"exit={record.get('exit_code')}.\n"
                f"stderr tail:\n```\n{record.get('stderr_tail', '')[:500]}\n```"
            )
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        log.warning("discord notify failed: %s", exc)


def should_fire(orchestration: Mapping[str, Any],
                event_id: str,
                *,
                now: Optional[datetime] = None,
                base: Optional[Path] = None) -> tuple[bool, str]:
    """Decide if this orchestration should fire NOW.

    Returns ``(fire, reason)``. ``reason`` is the skip reason when
    fire is False; a short tag when fire is True (useful in logs).
    """
    if _in_flight(event_id, base):
        return False, "in-flight"
    max_runs = int(orchestration.get("max_runs_per_day", 1) or 1)
    today_count = _completed_runs_today(event_id, base)
    if today_count >= max_runs:
        return False, f"max_runs_per_day={max_runs} reached"
    start_time = orchestration.get("start_time") or orchestration.get("when")
    parsed = _parse_hhmm(start_time)
    if parsed is None:
        # Some orchestrations may want to be manual-only; absent
        # start_time means "no scheduled trigger." Allow run-now.
        return False, "no start_time"
    target_h, target_m = parsed
    current = now if now is not None else datetime.now()
    cur_minutes = current.hour * 60 + current.minute
    target_minutes = target_h * 60 + target_m
    if cur_minutes < target_minutes:
        return False, f"before start_time {target_h:02d}:{target_m:02d}"
    catch_up = bool(orchestration.get("catch_up", False))
    if catch_up:
        return True, "catch-up: past start_time + no completed run today"
    if cur_minutes < target_minutes + 2:  # 2-minute fire window
        return True, "fire window: within 2 minutes of start_time"
    return False, (
        f"past start_time {target_h:02d}:{target_m:02d} but catch_up=False"
    )


def fire(orchestration: Mapping[str, Any], event_id: str,
         *,
         base: Optional[Path] = None,
         subprocess_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
         notify: Callable[[dict], None] = notify_failure_default) -> RunRecord:
    """Execute the orchestration's command, persist the run record.

    ``subprocess_runner`` is injectable for tests; defaults to
    ``subprocess.run`` with a 600s timeout and captured output.
    """
    record = RunRecord(
        event_id=event_id, started_at=_now_iso(),
        kind=str(orchestration.get("kind", "")),
    )
    _persist(record, base)
    runner = subprocess_runner or _default_runner
    try:
        proc = runner(orchestration["command"])
        record.exit_code = proc.returncode
        record.stdout_tail = (proc.stdout or "")[-2000:]
        record.stderr_tail = (proc.stderr or "")[-2000:]
        record.status = "success" if proc.returncode == 0 else "failure"
    except Exception as exc:
        record.exit_code = -1
        record.stderr_tail = f"runner raised: {type(exc).__name__}: {exc}"
        record.status = "failure"
    record.ended_at = _now_iso()
    _persist(record, base)
    if record.status == "failure":
        try:
            notify(record.to_dict())
        except Exception:
            log.warning("notify hook raised; suppressing")
    return record


def _default_runner(command: str) -> subprocess.CompletedProcess:
    # d48d4ddb: route through windowless wrapper so the calendar
    # scheduler's per-tick subprocess never flashes a cmd window.
    # Output is still captured (text=True + capture_output=True);
    # WINDOWLESS != silent.
    from windowless_subprocess import run as _wl_run
    return _wl_run(
        command, shell=True, capture_output=True, text=True, timeout=600,
    )


def tick(events: list[dict], *,
         now: Optional[datetime] = None,
         base: Optional[Path] = None,
         subprocess_runner: Optional[Callable] = None,
         notify: Callable[[dict], None] = notify_failure_default) -> list[RunRecord]:
    """One scheduler tick. Walks every event with orchestration; fires
    the ones whose ``should_fire`` returns True. Returns the run
    records produced this tick (empty list when nothing fired)."""
    fired: list[RunRecord] = []
    for event in events:
        eid = event.get("id")
        if not eid:
            continue
        orch = event_orchestration(event)
        if orch is None:
            continue
        ok, reason = should_fire(orch, eid, now=now, base=base)
        if not ok:
            log.debug("skip %s: %s", eid, reason)
            continue
        log.info("fire %s: %s", eid, reason)
        rec = fire(orch, eid, base=base,
                   subprocess_runner=subprocess_runner, notify=notify)
        fired.append(rec)
    return fired


# ---------------------------------------------------------------------------
# Loop runner (started by the calendar app on boot)
# ---------------------------------------------------------------------------

_loop_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _loop_body(load_events: Callable[[], list[dict]],
               interval: int = TICK_INTERVAL_SECONDS,
               base: Optional[Path] = None) -> None:
    while not _stop_event.wait(timeout=interval):
        try:
            tick(load_events(), base=base)
        except Exception as exc:  # never let the loop die
            log.exception("scheduler tick failed: %s", exc)


def start_loop(load_events: Callable[[], list[dict]],
               interval: int = TICK_INTERVAL_SECONDS,
               base: Optional[Path] = None) -> None:
    """Start the background scheduler thread. Idempotent."""
    global _loop_thread
    if _loop_thread is not None and _loop_thread.is_alive():
        return
    _stop_event.clear()
    _loop_thread = threading.Thread(
        target=_loop_body, args=(load_events, interval, base),
        name="calendar-scheduler", daemon=True,
    )
    _loop_thread.start()


def stop_loop(timeout: float = 5.0) -> None:
    global _loop_thread
    _stop_event.set()
    if _loop_thread is not None and _loop_thread.is_alive():
        _loop_thread.join(timeout=timeout)
    _loop_thread = None
