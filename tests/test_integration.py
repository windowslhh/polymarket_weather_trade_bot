"""End-to-end integration test for the full trading logic chain.

Simulates the complete pipeline with mock data:
  Market Discovery → Weather Forecast → Strategy Evaluation →
  Position Sizing → Order Execution → Portfolio Recording →
  Rebalance (Exit signals) → Risk Management

This test does NOT call external APIs. All external dependencies are mocked.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, CityConfig, SchedulingConfig, StrategyConfig
from src.execution.executor import Executor
from src.markets.clob_client import ClobClient, OrderResult
from src.markets.models import Side, TempSlot, TokenType, TradeSignal, WeatherMarketEvent
from src.portfolio.risk import check_circuit_breaker, check_exposure_limits, check_geographic_correlation
from src.portfolio.store import Store
from src.portfolio.tracker import PortfolioTracker
from src.strategy.evaluator import evaluate_exit_signals, evaluate_no_signals, evaluate_yes_signals
from src.strategy.rebalancer import Rebalancer
from src.strategy.sizing import compute_size
from src.weather.metar import DailyMaxTracker
from src.weather.models import Forecast, Observation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_config(dry_run: bool = True) -> AppConfig:
    return AppConfig(
        strategy=StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.01,
            yes_confirmation_threshold=0.80,
            max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0,
            max_total_exposure_usd=1000.0,
            daily_loss_limit_usd=50.0,
            kelly_fraction=0.5,
        ),
        scheduling=SchedulingConfig(),
        cities=[
            CityConfig("New York", "KLGA", 40.7128, -74.006),
            CityConfig("Dallas", "KDAL", 32.7767, -96.797),
            CityConfig("Seattle", "KSEA", 47.6062, -122.332),
        ],
        dry_run=dry_run,
        db_path=Path("/tmp/test_bot_integration.db"),
    )


def _make_market_event(city: str, slots: list[TempSlot], hours_until_end: float = 12.0) -> WeatherMarketEvent:
    return WeatherMarketEvent(
        event_id=f"evt_{city.lower().replace(' ', '_')}",
        condition_id=f"cond_{city.lower().replace(' ', '_')}",
        city=city,
        market_date=date.today(),
        slots=slots,
        end_timestamp=datetime.now(timezone.utc) + timedelta(hours=hours_until_end),
        title=f"Highest temperature in {city} on {date.today().strftime('%B %d')}",
    )


def _make_slots_for_city(forecast_high: float) -> list[TempSlot]:
    """Create realistic temperature slots spanning a range around the forecast."""
    slots = []
    # Create slots: each 2°F wide, from forecast-20 to forecast+20
    base = int(forecast_high) - 20
    for i in range(20):
        lower = base + i * 2
        upper = lower + 2
        # Price NO: higher for slots far from forecast, lower for close ones
        distance = abs((lower + upper) / 2 - forecast_high)
        price_no = min(0.98, 0.50 + distance * 0.03)
        price_yes = round(1.0 - price_no, 4)
        slots.append(TempSlot(
            token_id_yes=f"yes_{lower}_{upper}",
            token_id_no=f"no_{lower}_{upper}",
            outcome_label=f"{lower}°F to {upper}°F",
            temp_lower_f=float(lower),
            temp_upper_f=float(upper),
            price_yes=round(price_yes, 4),
            price_no=round(price_no, 4),
        ))
    return slots


# ── Test: Full Logic Chain (Unit-level, no mocks needed) ────────────────

class TestFullLogicChain:
    """Test the complete strategy logic chain with synthetic data, no I/O."""

    def test_step1_no_signal_generation(self):
        """Step 1: Evaluator generates NO signals for slots far from forecast."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01)
        forecast_high = 75.0
        slots = _make_slots_for_city(forecast_high)
        event = _make_market_event("New York", slots)

        forecast = Forecast(
            city="New York", forecast_date=date.today(),
            predicted_high_f=forecast_high, predicted_low_f=60.0,
            confidence_interval_f=4.0, source="test",
            fetched_at=datetime.now(timezone.utc),
        )

        signals = evaluate_no_signals(event, forecast, config)

        logger.info("=== Step 1: NO Signal Generation ===")
        logger.info("Forecast high: %.1f°F, Total slots: %d", forecast_high, len(slots))
        logger.info("NO signals generated: %d", len(signals))
        for s in signals:
            logger.info(
                "  → BUY NO %s | price=%.4f | win_prob=%.4f | EV=%.4f",
                s.slot.outcome_label, s.price, s.estimated_win_prob, s.expected_value,
            )

        assert len(signals) > 0, "Should generate at least some NO signals"
        assert all(s.token_type == TokenType.NO for s in signals)
        assert all(s.side == Side.BUY for s in signals)
        assert all(s.expected_value >= 0.01 for s in signals)

        # All signaled slots should be far from forecast
        for s in signals:
            mid = (s.slot.temp_lower_f + s.slot.temp_upper_f) / 2
            assert abs(mid - forecast_high) >= 8, f"Slot {s.slot.outcome_label} too close to forecast"

    def test_step2_position_sizing(self):
        """Step 2: Sizing respects Kelly criterion and exposure caps."""
        config = StrategyConfig(
            max_position_per_slot_usd=5.0,
            max_exposure_per_city_usd=50.0,
            max_total_exposure_usd=1000.0,
            kelly_fraction=0.5,
        )

        # High EV signal
        slot = TempSlot("y1", "n1", "90°F to 92°F", 90, 92, 0.05, 0.95)
        event = _make_market_event("New York", [slot])
        signal = TradeSignal(TokenType.NO, Side.BUY, slot, event, 0.04, 0.97)

        size = compute_size(signal, city_exposure_usd=0, total_exposure_usd=0, config=config)

        logger.info("=== Step 2: Position Sizing ===")
        logger.info("Signal: NO %s @ %.4f, win_prob=%.4f, EV=%.4f", slot.outcome_label, slot.price_no, 0.97, 0.04)
        logger.info("Computed size: $%.2f", size)

        assert size > 0, "High EV signal should get positive size"
        assert size <= 5.0, "Should not exceed per-slot cap"

        # Test: city at capacity
        size_maxed = compute_size(signal, city_exposure_usd=50.0, total_exposure_usd=50.0, config=config)
        assert size_maxed == 0.0, "Should return 0 when city is maxed"
        logger.info("Size when city maxed: $%.2f ✓", size_maxed)

        # Test: global at capacity
        size_global = compute_size(signal, city_exposure_usd=0, total_exposure_usd=1000.0, config=config)
        assert size_global == 0.0, "Should return 0 when global is maxed"
        logger.info("Size when global maxed: $%.2f ✓", size_global)

    def test_step3_exit_signals(self):
        """Step 3: Exit signals fire when real-time temp approaches held NO slot."""
        config = StrategyConfig(no_distance_threshold_f=8)

        # We hold NO on 85-87°F slot
        held_slot = TempSlot("y1", "n1", "85°F to 87°F", 85, 87, 0.1, 0.9)
        event = _make_market_event("New York", [held_slot])

        # Scenario A: temp is 84°F — danger zone (within threshold/2 = 4)
        obs_danger = Observation("KLGA", 84.0, datetime.now(timezone.utc))
        exits = evaluate_exit_signals(event, obs_danger, 84.0, [held_slot], config)

        logger.info("=== Step 3: Exit Signals ===")
        logger.info("Held NO slot: %s", held_slot.outcome_label)
        logger.info("Scenario A: daily max = 84°F (danger) → exits: %d", len(exits))
        assert len(exits) == 1, "Should signal exit when temp approaches slot"
        assert exits[0].side == Side.SELL

        # Scenario B: temp is 70°F — safe
        obs_safe = Observation("KLGA", 70.0, datetime.now(timezone.utc))
        exits_safe = evaluate_exit_signals(event, obs_safe, 70.0, [held_slot], config)
        logger.info("Scenario B: daily max = 70°F (safe)   → exits: %d ✓", len(exits_safe))
        assert len(exits_safe) == 0

    def test_step4_yes_signals_late_game(self):
        """Step 4: YES signals trigger when temp is confirmed near market close."""
        config = StrategyConfig(yes_confirmation_threshold=0.80)

        # Slot 74-76°F, market closes in 1 hour
        slot = TempSlot("y1", "n1", "74°F to 76°F", 74, 76, 0.60, 0.40)
        event = _make_market_event("New York", [slot], hours_until_end=0.8)

        forecast = Forecast("New York", date.today(), 75.0, 60.0, 4.0, "test", datetime.now(timezone.utc))
        obs = Observation("KLGA", 75.0, datetime.now(timezone.utc))

        signals = evaluate_yes_signals(event, forecast, obs, 75.0, config)

        logger.info("=== Step 4: YES Signals (Late Game) ===")
        logger.info("Slot: %s, YES price: %.2f, hours left: 0.8", slot.outcome_label, slot.price_yes)
        logger.info("Daily max: 75°F (in slot range 74-76°F)")
        logger.info("YES signals: %d", len(signals))

        assert len(signals) == 1, "Should generate YES signal when temp confirmed"
        assert signals[0].token_type == TokenType.YES
        assert signals[0].estimated_win_prob >= 0.85
        logger.info("  → BUY YES %s | prob=%.4f | EV=%.4f ✓", signals[0].slot.outcome_label, signals[0].estimated_win_prob, signals[0].expected_value)

        # Scenario: too early (12 hours left) — no signal
        event_early = _make_market_event("New York", [slot], hours_until_end=12)
        signals_early = evaluate_yes_signals(event_early, forecast, obs, 75.0, config)
        assert len(signals_early) == 0, "Should not signal YES when too much time left"
        logger.info("12 hours remaining → no YES signal ✓")

    def test_step5_risk_management(self):
        """Step 5: Risk checks prevent over-exposure."""
        config = _make_config()

        logger.info("=== Step 5: Risk Management ===")

        # Circuit breaker
        assert not check_circuit_breaker(None, config), "No P&L data → no breaker"
        assert not check_circuit_breaker(-10.0, config), "Small loss → no breaker"
        assert check_circuit_breaker(-60.0, config), "Large loss → breaker triggered"
        logger.info("Circuit breaker: ✓")

        # Exposure limits
        assert not check_exposure_limits(5.0, 0, 0, config), "Fresh start → ok"
        assert check_exposure_limits(5.0, 48.0, 48.0, config), "City nearly maxed → blocked"
        assert check_exposure_limits(5.0, 0, 998.0, config), "Global nearly maxed → blocked"
        logger.info("Exposure limits: ✓")

        # Geographic correlation
        warnings = check_geographic_correlation(
            ["New York", "Dallas", "Seattle"],
            config.cities,
            distance_threshold_km=500.0,
        )
        logger.info("Correlation warnings (500km threshold): %d pairs", len(warnings))
        for c1, c2, dist in warnings:
            logger.info("  ⚠ %s ↔ %s: %.0f km", c1, c2, dist)
        # NY, Dallas, Seattle are all >500km apart
        assert len(warnings) == 0, "These cities should not be correlated"

    def test_step6_multi_city_diversification(self):
        """Step 6: Strategy generates signals across multiple cities."""
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.01)

        cities_data = [
            ("New York", 75.0),
            ("Dallas", 88.0),
            ("Seattle", 62.0),
        ]

        logger.info("=== Step 6: Multi-City Diversification ===")
        total_signals = 0

        for city, high in cities_data:
            slots = _make_slots_for_city(high)
            event = _make_market_event(city, slots)
            forecast = Forecast(city, date.today(), high, high - 15, 4.0, "test", datetime.now(timezone.utc))
            signals = evaluate_no_signals(event, forecast, config)
            total_signals += len(signals)
            logger.info("  %s (forecast: %.0f°F): %d NO signals", city, high, len(signals))

        assert total_signals > 0, "Should have signals across cities"
        logger.info("Total signals across 3 cities: %d ✓", total_signals)


# ── Test: Integration with SQLite Portfolio ─────────────────────────────

class TestPortfolioIntegration:
    """Test that signals flow through to actual SQLite position records."""

    @pytest.fixture
    async def portfolio(self, tmp_path):
        store = Store(tmp_path / "test.db")
        await store.initialize()
        tracker = PortfolioTracker(store)
        yield tracker
        await store.close()

    @pytest.mark.asyncio
    async def test_full_flow_discovery_to_portfolio(self, portfolio):
        """Complete chain: evaluate signals → size → execute (dry run) → record in DB."""
        config = _make_config(dry_run=True)
        clob = ClobClient(config)
        executor = Executor(clob, portfolio)

        # Simulate market data
        forecast_high = 75.0
        slots = _make_slots_for_city(forecast_high)
        event = _make_market_event("New York", slots)
        forecast = Forecast("New York", date.today(), forecast_high, 60.0, 4.0, "test", datetime.now(timezone.utc))

        # Step 1: Evaluate
        no_signals = evaluate_no_signals(event, forecast, config.strategy)
        assert len(no_signals) > 0

        logger.info("=== Portfolio Integration Test ===")
        logger.info("NO signals: %d", len(no_signals))

        # Step 2: Size
        sized_signals = []
        total_exposure = 0.0
        city_exposure = 0.0
        for signal in no_signals:
            size = compute_size(signal, city_exposure, total_exposure, config.strategy)
            if size > 0:
                signal.suggested_size_usd = size
                sized_signals.append(signal)
                city_exposure += size
                total_exposure += size

        logger.info("Sized signals: %d (total exposure: $%.2f)", len(sized_signals), total_exposure)
        assert len(sized_signals) > 0

        # Step 3: Execute (dry run — records in portfolio but doesn't call CLOB)
        await executor.execute_signals(sized_signals)

        # Step 4: Verify portfolio state
        db_exposure = await portfolio.get_total_exposure()
        city_exp = await portfolio.get_city_exposure("New York")
        held_slots = await portfolio.get_held_no_slots(event.event_id)

        logger.info("DB total exposure: $%.2f", db_exposure)
        logger.info("DB city exposure (NYC): $%.2f", city_exp)
        logger.info("DB held NO slots: %d", len(held_slots))

        assert db_exposure > 0, "Portfolio should have recorded positions"
        assert city_exp > 0
        assert len(held_slots) == len(sized_signals)
        assert db_exposure == pytest.approx(total_exposure, abs=0.01)

        logger.info("✓ All positions correctly recorded in SQLite")

    @pytest.mark.asyncio
    async def test_circuit_breaker_halts_rebalancer(self, portfolio):
        """Rebalancer should abort when daily P&L exceeds loss limit."""
        config = _make_config(dry_run=True)
        clob = ClobClient(config)
        executor = Executor(clob, portfolio)
        max_tracker = DailyMaxTracker()

        # Inject a large daily loss
        await portfolio._store.upsert_daily_pnl(date.today().isoformat(), -100.0, 0, 0)

        rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker)

        # Mock discover_weather_markets so we don't hit real API
        with patch("src.strategy.rebalancer.discover_weather_markets", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = [_make_market_event("New York", _make_slots_for_city(75.0))]
            signals = await rebalancer.run()

        logger.info("=== Circuit Breaker Test ===")
        logger.info("Daily P&L: -$100 (limit: -$50)")
        logger.info("Signals generated: %d", len(signals))
        assert len(signals) == 0, "Circuit breaker should halt trading"
        logger.info("✓ Circuit breaker correctly halted rebalancer")

    @pytest.mark.asyncio
    async def test_rebalancer_full_cycle_mocked(self, portfolio):
        """Full rebalancer cycle with mocked external APIs."""
        config = _make_config(dry_run=True)
        clob = ClobClient(config)
        executor = Executor(clob, portfolio)
        max_tracker = DailyMaxTracker()

        forecast_high = 78.0
        events = [
            _make_market_event("New York", _make_slots_for_city(75.0)),
            _make_market_event("Dallas", _make_slots_for_city(88.0)),
        ]
        forecasts = {
            "New York": Forecast("New York", date.today(), 75.0, 60.0, 4.0, "mock", datetime.now(timezone.utc)),
            "Dallas": Forecast("Dallas", date.today(), 88.0, 72.0, 4.0, "mock", datetime.now(timezone.utc)),
        }

        rebalancer = Rebalancer(config, clob, portfolio, executor, max_tracker)

        with (
            patch("src.strategy.rebalancer.discover_weather_markets", new_callable=AsyncMock) as mock_discover,
            patch("src.strategy.rebalancer.get_forecasts_batch", new_callable=AsyncMock) as mock_forecasts,
            patch("src.strategy.rebalancer.fetch_settlement_temp", new_callable=AsyncMock) as mock_settle,
            patch("src.strategy.rebalancer.validate_station_config", return_value=[]),
        ):
            mock_discover.return_value = events
            mock_forecasts.return_value = forecasts
            mock_settle.return_value = None  # No observation data (just forecast-based trading)

            signals = await rebalancer.run()

        logger.info("=== Full Rebalancer Cycle (Mocked) ===")
        logger.info("Events: %d cities", len(events))
        logger.info("Signals generated: %d", len(signals))

        assert len(signals) > 0, "Should generate signals for 2 cities"

        # Check we have signals for both cities
        signal_cities = {s.event.city for s in signals}
        logger.info("Cities with signals: %s", signal_cities)

        total_exp = await portfolio.get_total_exposure()
        ny_exp = await portfolio.get_city_exposure("New York")
        dal_exp = await portfolio.get_city_exposure("Dallas")

        logger.info("Exposure — Total: $%.2f, NYC: $%.2f, Dallas: $%.2f", total_exp, ny_exp, dal_exp)
        assert total_exp > 0
        logger.info("✓ Full rebalancer cycle completed successfully")


# ── Test: METAR daily max tracking integration ──────────────────────────

class TestMetarDailyMaxIntegration:
    def test_daily_max_drives_exit_decisions(self):
        """METAR daily max correctly triggers exit signals for threatened positions."""
        tracker = DailyMaxTracker()
        config = StrategyConfig(no_distance_threshold_f=8)

        held_slot = TempSlot("y1", "n1", "82°F to 84°F", 82, 84, 0.1, 0.9)
        event = _make_market_event("New York", [held_slot])

        logger.info("=== METAR → Exit Signal Integration ===")
        logger.info("Held NO slot: %s", held_slot.outcome_label)

        # Morning: 68°F — safe
        obs1 = Observation("KLGA", 68.0, datetime(2026, 4, 4, 10, 0, tzinfo=timezone.utc))
        max1 = tracker.update(obs1)
        exits1 = evaluate_exit_signals(event, obs1, max1, [held_slot], config)
        logger.info("10:00 UTC — temp 68°F, max %.1f°F → exits: %d", max1, len(exits1))
        assert len(exits1) == 0

        # Afternoon: 78°F — still safe but warming
        obs2 = Observation("KLGA", 78.0, datetime(2026, 4, 4, 14, 0, tzinfo=timezone.utc))
        max2 = tracker.update(obs2)
        exits2 = evaluate_exit_signals(event, obs2, max2, [held_slot], config)
        logger.info("14:00 UTC — temp 78°F, max %.1f°F → exits: %d", max2, len(exits2))
        assert len(exits2) == 0

        # Peak: 81°F — danger zone!
        obs3 = Observation("KLGA", 81.0, datetime(2026, 4, 4, 16, 0, tzinfo=timezone.utc))
        max3 = tracker.update(obs3)
        exits3 = evaluate_exit_signals(event, obs3, max3, [held_slot], config)
        logger.info("16:00 UTC — temp 81°F, max %.1f°F → exits: %d 🚨", max3, len(exits3))
        assert len(exits3) == 1, "Should exit when daily max approaches slot"

        logger.info("✓ METAR tracking correctly drives exit decisions")
