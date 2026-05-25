"""SCHEDULER-LIFESPAN-WIRE1 regression: the FastAPI lifespan must
actually start (and stop) the scheduler tick thread. Prior to the
fix, `_lifespan` called `_bootstrap()` but never invoked
`scheduler.start_loop()`, leaving every scheduled event dormant
since 994deef5 shipped."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CALENDAR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALENDAR_ROOT))


@pytest.mark.asyncio
async def test_lifespan_starts_scheduler_thread(monkeypatch, tmp_path):
    """Entering the lifespan ctx should populate
    `scheduler._loop_thread` with a live Thread."""
    import scheduler as sched
    monkeypatch.setattr(sched, "TICK_INTERVAL_SECONDS", 1)
    sched._loop_thread = None  # ensure clean slate

    import app
    async with app._lifespan(app.app):
        assert sched._loop_thread is not None, (
            "lifespan should have started the scheduler thread"
        )
        assert sched._loop_thread.is_alive(), "thread should be running"

    # On exit, stop_loop runs; thread should be reset to None.
    assert sched._loop_thread is None, (
        "lifespan exit should have stopped the scheduler thread"
    )


@pytest.mark.asyncio
async def test_lifespan_stops_scheduler_on_shutdown(monkeypatch):
    """Verify the finally-branch runs even if the yielded block
    raises — graceful shutdown contract."""
    import scheduler as sched
    monkeypatch.setattr(sched, "TICK_INTERVAL_SECONDS", 1)
    sched._loop_thread = None

    import app
    with pytest.raises(RuntimeError):
        async with app._lifespan(app.app):
            assert sched._loop_thread is not None
            raise RuntimeError("simulate handler crash")

    assert sched._loop_thread is None, (
        "stop_loop should run in finally even after exception"
    )
