"""FIX-06: scheduler jobs must have coalesce + misfire_grace_time,
and a job-error listener must route critical alerts through Alerter.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

from src.alerts import Alerter
from src.scheduler.jobs import JOB_COALESCE, JOB_MISFIRE_GRACE_S, setup_scheduler


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(
        scheduling=SimpleNamespace(rebalance_interval_minutes=60),
    )


def test_all_jobs_have_coalesce_and_grace():
    rebalancer = MagicMock()
    scheduler = setup_scheduler(_make_config(), rebalancer, alerter=None)

    for job in scheduler.get_jobs():
        assert job.coalesce is JOB_COALESCE, f"{job.id} missing coalesce"
        # APScheduler exposes misfire_grace_time on the Job directly.
        assert job.misfire_grace_time == JOB_MISFIRE_GRACE_S, (
            f"{job.id} misfire_grace_time={job.misfire_grace_time} (want {JOB_MISFIRE_GRACE_S})"
        )


def test_error_listener_is_registered():
    rebalancer = MagicMock()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    scheduler = setup_scheduler(_make_config(), rebalancer, alerter=alerter)

    listeners = scheduler._listeners  # type: ignore[attr-defined]
    assert any(callable(cb) for cb, _mask in listeners), (
        "FIX-06 job-error listener was not registered"
    )


@pytest.mark.asyncio
async def test_error_listener_fires_alerter_on_real_event():
    """Review 🟡 #4: verify the listener actually calls Alerter.send
    when APScheduler dispatches an EVENT_JOB_ERROR.  Previously the
    test only asserted a listener existed, not that it did anything.
    """
    rebalancer = MagicMock()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    scheduler = setup_scheduler(_make_config(), rebalancer, alerter=alerter)

    # Find our listener (the tuple is (callback, event_mask)).
    listeners = scheduler._listeners  # type: ignore[attr-defined]
    assert listeners, "no listener registered"
    callback, _mask = listeners[0]

    # Build a real JobExecutionEvent carrying an exception, mimicking
    # what APScheduler hands the callback on a job error.
    from datetime import datetime, timezone
    event = JobExecutionEvent(
        code=EVENT_JOB_ERROR,
        job_id="rebalance",
        jobstore="default",
        scheduled_run_time=datetime.now(timezone.utc),
        retval=None,
        exception=RuntimeError("simulated rebalance boom"),
        traceback="Traceback (most recent call last):\n  ...\nRuntimeError: …",
    )

    # Listener calls loop.create_task(alerter.send(...)) — must be
    # invoked from inside a running loop.
    callback(event)

    # Give the event loop one turn so the create_task actually awaits send.
    await asyncio.sleep(0)

    alerter.send.assert_called_once()
    level, msg = alerter.send.call_args[0]
    assert level == "critical"
    assert "rebalance" in msg
    assert "simulated rebalance boom" in msg


# ──────────────────────────────────────────────────────────────────────
# G-1' periodic reconciler
# ──────────────────────────────────────────────────────────────────────


def test_reconciler_periodic_job_registered_in_live_mode():
    """G-1': when caller passes a query_clob_order callable, the
    scheduler must include a 30-min reconciler_periodic job.  Pre-fix
    the reconciler ran only at startup."""
    from src.scheduler.jobs import RECONCILER_INTERVAL_MINUTES

    rebalancer = MagicMock()
    rebalancer._cycle_lock = asyncio.Lock()

    async def _probe(_row):
        return None  # never actually called in this test

    scheduler = setup_scheduler(
        _make_config(), rebalancer, alerter=None,
        query_clob_order=_probe, is_paper=False,
    )
    job = scheduler.get_job("reconciler_periodic")
    assert job is not None, "G-1': reconciler_periodic must be scheduled"
    # 30 min interval (RECONCILER_INTERVAL_MINUTES default)
    assert job.trigger.interval.total_seconds() == RECONCILER_INTERVAL_MINUTES * 60
    # Inherits misfire/coalesce settings same as everything else
    assert job.coalesce is JOB_COALESCE
    assert job.misfire_grace_time == JOB_MISFIRE_GRACE_S


def test_reconciler_periodic_job_registered_in_paper_mode():
    """G-1': paper mode also benefits from the periodic reconciler
    (it sweeps stale `pending` rows)."""
    rebalancer = MagicMock()
    rebalancer._cycle_lock = asyncio.Lock()
    scheduler = setup_scheduler(
        _make_config(), rebalancer, alerter=None,
        query_clob_order=None, is_paper=True,
    )
    assert scheduler.get_job("reconciler_periodic") is not None


def test_reconciler_skipped_when_no_query_callable_and_not_paper():
    """G-1': defensive — if the caller didn't wire query_clob_order
    AND we're not in paper, the periodic reconciler does NOT register.
    Avoids running an unconfigured reconciler that could mark all
    pending orders failed for no reason."""
    rebalancer = MagicMock()
    rebalancer._cycle_lock = asyncio.Lock()
    scheduler = setup_scheduler(
        _make_config(), rebalancer, alerter=None,
        query_clob_order=None, is_paper=False,
    )
    assert scheduler.get_job("reconciler_periodic") is None


@pytest.mark.asyncio
async def test_reconciler_periodic_acquires_cycle_lock():
    """G-1' invariant: the periodic reconciler must hold rebalancer's
    cycle_lock while it runs so its DB writes don't interleave with
    a rebalance/position_check.  We pin this by checking that the
    function tries to acquire the same lock the rebalancer uses."""
    rebalancer = MagicMock()
    cycle_lock = asyncio.Lock()
    rebalancer._cycle_lock = cycle_lock
    rebalancer._portfolio.store = MagicMock()

    # Drive the periodic wrapper directly.  Patch reconcile_pending_orders
    # to record whether the lock was held when called.
    lock_held_during_call = {"v": None}

    async def _capture_reconcile(*a, **kw):
        lock_held_during_call["v"] = cycle_lock.locked()
        return None

    from src.scheduler import jobs as jobs_mod
    from unittest.mock import patch

    async def _probe(_row):
        return None

    with patch.object(jobs_mod, "reconcile_pending_orders", _capture_reconcile):
        scheduler = setup_scheduler(
            _make_config(), rebalancer, alerter=None,
            query_clob_order=_probe, is_paper=False,
        )
        # Pull the job's wrapped callable and invoke it directly
        job = scheduler.get_job("reconciler_periodic")
        await job.func()

    assert lock_held_during_call["v"] is True, (
        "G-1': reconcile_pending_orders must run inside the cycle_lock"
    )


@pytest.mark.asyncio
async def test_reconciler_periodic_uses_exit_on_mismatch_false():
    """G-1' invariant: at runtime the reconciler must NOT sys.exit on
    mismatch (only safe at startup).  Pin via the call kwargs."""
    rebalancer = MagicMock()
    rebalancer._cycle_lock = asyncio.Lock()
    rebalancer._portfolio.store = MagicMock()

    captured_kwargs: dict = {}

    async def _capture(**kw):
        captured_kwargs.update(kw)
        return None

    from src.scheduler import jobs as jobs_mod
    from unittest.mock import patch

    async def _probe(_row):
        return None

    with patch.object(jobs_mod, "reconcile_pending_orders", _capture):
        scheduler = setup_scheduler(
            _make_config(), rebalancer, alerter=None,
            query_clob_order=_probe, is_paper=False,
        )
        job = scheduler.get_job("reconciler_periodic")
        await job.func()

    assert captured_kwargs.get("exit_on_mismatch") is False, (
        "G-1': periodic reconciler must NOT sys.exit at runtime; "
        "exit_on_mismatch=True is only safe at startup"
    )
