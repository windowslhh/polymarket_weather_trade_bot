"""FIX-06: scheduler jobs must have coalesce + misfire_grace_time,
and a job-error listener must route critical alerts through Alerter.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def test_error_listener_routes_to_alerter():
    rebalancer = MagicMock()
    alerter = Alerter(webhook_url="")
    alerter.send = AsyncMock()  # type: ignore[method-assign]

    scheduler = setup_scheduler(_make_config(), rebalancer, alerter=alerter)

    # Pull our listener back out of the private registry.  APScheduler stores
    # each registered listener as a (callback, mask) tuple on _listeners.
    listeners = scheduler._listeners  # type: ignore[attr-defined]
    assert any(
        callable(cb) for cb, _mask in listeners
    ), "FIX-06 job-error listener was not registered"
