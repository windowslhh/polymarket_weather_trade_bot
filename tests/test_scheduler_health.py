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
