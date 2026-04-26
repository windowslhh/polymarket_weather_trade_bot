"""FIX-22: meta-tests enforcing the forecast/market_date invariant.

Bug #1 (Houston 2026-04-17) was caused by passing today's forecast into a
D+1/D+2 evaluator — the "model says 98% win" signal was based on the wrong
forecast_date.  FIX-22 adds asserts to each evaluator entry point; this
module catches anyone who adds a new evaluator and forgets the assert.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

EVALUATOR_PATH = Path(__file__).resolve().parents[1] / "src" / "strategy" / "evaluator.py"


def _collect_evaluator_defs() -> list[ast.FunctionDef]:
    tree = ast.parse(EVALUATOR_PATH.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("evaluate_")
        and node.name.endswith("_signals")
    ]


def _function_takes_forecast_param(fn: ast.FunctionDef) -> bool:
    """True iff the function declares a ``forecast`` parameter (positional,
    kw-only, or default).  Only these functions need the FIX-22 assert —
    evaluate_locked_win_signals is observation-driven and has no forecast_date
    to validate.
    """
    all_args = list(fn.args.args) + list(fn.args.kwonlyargs)
    return any(a.arg == "forecast" for a in all_args)


def _function_has_date_assert(fn: ast.FunctionDef) -> bool:
    """True iff the function body contains an ``assert`` OR an
    ``if … raise AssertionError`` whose source references both
    forecast_date and market_date.  Accepts both forms because the
    ``if … raise`` variant survives `python -O` which strips asserts.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            text = ast.unparse(node)
            if "forecast_date" in text and "market_date" in text:
                return True
        # `if <cond>: raise AssertionError(...)` — walk into the If body
        # and look for AssertionError / Exception raises whose surrounding
        # text mentions the two names.
        if isinstance(node, ast.If):
            text = ast.unparse(node)
            if (
                "forecast_date" in text
                and "market_date" in text
                and "raise" in text
            ):
                return True
    return False


def test_all_evaluator_entrypoints_have_date_assert():
    """Every evaluate_*_signals that takes a forecast must assert the date."""
    fns = _collect_evaluator_defs()
    assert fns, "Did not find any evaluate_*_signals — probably a parse bug"

    forecast_consumers = [fn for fn in fns if _function_takes_forecast_param(fn)]
    assert forecast_consumers, (
        "Expected at least one evaluator that takes a forecast — "
        "check parse logic"
    )

    missing = [
        fn.name for fn in forecast_consumers if not _function_has_date_assert(fn)
    ]
    assert not missing, (
        "FIX-22 regression: these evaluator entrypoints do not assert "
        f"forecast.forecast_date == event.market_date: {missing}. "
        "See CLAUDE.md review checklist."
    )


@pytest.mark.parametrize(
    "func_name", ["evaluate_no_signals", "evaluate_trim_signals", "evaluate_exit_signals"],
)
def test_forecast_date_assert_fires_on_mismatch(func_name):
    """Runtime sanity: passing a mismatched forecast into evaluator raises AssertionError.

    This complements the AST scan — the static check catches "assert exists",
    this check catches "assert is actually the right invariant".
    """
    from datetime import date, datetime
    from src.config import StrategyConfig
    from src.markets.models import TempSlot, WeatherMarketEvent
    from src.strategy import evaluator
    from src.weather.models import Forecast, Observation

    event = WeatherMarketEvent(
        event_id="e", condition_id="c", city="Chicago",
        market_date=date(2026, 4, 25),
        slots=[TempSlot(
            token_id_yes="y", token_id_no="n", outcome_label="80°F",
            temp_lower_f=80.0, temp_upper_f=80.0, price_no=0.5,
        )],
    )
    wrong_forecast = Forecast(
        city="Chicago",
        forecast_date=date(2026, 4, 26),  # deliberately off by one
        predicted_high_f=82.0, predicted_low_f=60.0,
        confidence_interval_f=3.0, source="test",
        fetched_at=datetime.now(),
    )
    cfg = StrategyConfig()
    func = getattr(evaluator, func_name)

    with pytest.raises(AssertionError, match="forecast_date"):
        if func_name == "evaluate_exit_signals":
            obs = Observation(icao="KORD", temp_f=70.0, observation_time=datetime.now())
            func(event=event, observation=obs, daily_max_f=70.0,
                 held_no_slots=[], config=cfg, forecast=wrong_forecast)
        elif func_name == "evaluate_trim_signals":
            func(event=event, forecast=wrong_forecast, held_no_slots=[], config=cfg)
        else:  # evaluate_no_signals
            func(event=event, forecast=wrong_forecast, config=cfg)
