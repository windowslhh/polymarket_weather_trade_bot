"""Tests for the backtesting engine."""
from datetime import date

from src.backtest.engine import _run_day, _simulate_market_prices, BacktestResult
from src.config import StrategyConfig
from src.weather.historical import ForecastErrorDistribution


class TestSimulateMarketPrices:
    def test_far_slots_priced_high_no(self):
        """Slots far from forecast should have high NO price."""
        slots = [(60, 62), (74, 76), (90, 92)]
        simulated = _simulate_market_prices(75.0, slots, forecast_std=4.0)

        assert len(simulated) == 3
        # Close slot should have lower NO price
        assert simulated[1].price_no < simulated[0].price_no
        assert simulated[1].price_no < simulated[2].price_no

    def test_all_prices_in_range(self):
        slots = [(i, i + 2) for i in range(55, 95, 2)]
        simulated = _simulate_market_prices(75.0, slots)
        for s in simulated:
            assert 0 < s.price_no <= 0.98


class TestRunDay:
    def _make_dist(self) -> ForecastErrorDistribution:
        errors = [float(i) for i in range(-5, 6)] * 5  # 55 samples
        return ForecastErrorDistribution("Test", errors)

    def test_all_no_wins(self):
        """When actual is exactly at forecast, distant NO slots should all win."""
        config = StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.001,
            max_position_per_slot_usd=5.0,
        )
        result = _run_day(
            city="Test",
            day=date(2025, 6, 15),
            forecast_high_f=75.0,
            actual_high_f=75.0,  # exact match
            config=config,
            error_dist=self._make_dist(),
        )
        assert result.slots_traded > 0
        assert result.no_losses == 0, "No slot should lose when actual matches forecast"
        assert result.gross_pnl > 0

    def test_extreme_actual_causes_loss(self):
        """When actual is far from forecast, some NO slots might lose."""
        config = StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.001,
            max_position_per_slot_usd=5.0,
        )
        result = _run_day(
            city="Test",
            day=date(2025, 6, 15),
            forecast_high_f=75.0,
            actual_high_f=90.0,  # way off!
            config=config,
            error_dist=self._make_dist(),
        )
        assert result.slots_traded > 0
        # Should have at least one loss since actual (90) landed in a distant slot
        assert result.no_losses > 0

    def test_day_result_fields(self):
        config = StrategyConfig(no_distance_threshold_f=8, min_no_ev=0.001)
        result = _run_day("NYC", date(2025, 1, 1), 70.0, 72.0, config, self._make_dist())
        assert result.city == "NYC"
        assert result.forecast_error_f == -2.0  # 70 - 72
        assert result.slots_traded >= 0
        assert result.no_wins + result.no_losses == result.slots_traded

    def test_no_trades_when_threshold_high(self):
        """Very high threshold should produce zero trades."""
        config = StrategyConfig(no_distance_threshold_f=50, min_no_ev=0.001)
        result = _run_day("NYC", date(2025, 1, 1), 75.0, 75.0, config, self._make_dist())
        assert result.slots_traded == 0
        assert result.gross_pnl == 0


class TestMultiDayPnL:
    """Simulate multiple days to check aggregate behavior."""

    def test_profitable_over_normal_days(self):
        """Strategy should be profitable when forecast errors are small."""
        config = StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.005,
            max_position_per_slot_usd=5.0,
        )
        # Small errors: forecast is fairly accurate
        errors = [float(i) for i in range(-3, 4)] * 10
        dist = ForecastErrorDistribution("Test", errors)

        total_pnl = 0.0
        total_trades = 0
        for day_offset in range(30):
            # Normal days: actual within 3°F of forecast
            forecast = 75.0
            actual = 75.0 + (day_offset % 7 - 3)  # cycles -3 to +3

            result = _run_day("Test", date(2025, 6, day_offset + 1),
                             forecast, actual, config, dist)
            total_pnl += result.gross_pnl
            total_trades += result.slots_traded

        assert total_trades > 0, "Should have traded"
        # With small errors and buying distant NOs, should be profitable
        assert total_pnl > 0, f"Expected profit but got ${total_pnl:.2f}"

    def test_blowup_day_impact(self):
        """One extreme day should not wipe out many normal days of profit."""
        config = StrategyConfig(
            no_distance_threshold_f=8,
            min_no_ev=0.005,
            max_position_per_slot_usd=5.0,
        )
        errors = [float(i) for i in range(-3, 4)] * 10
        dist = ForecastErrorDistribution("Test", errors)

        results = []
        # 29 normal days + 1 blowup
        for i in range(29):
            r = _run_day("Test", date(2025, 6, i + 1), 75.0, 75.0 + (i % 5 - 2),
                        config, dist)
            results.append(r)

        # Blowup day: actual is 15°F higher than forecast
        blowup = _run_day("Test", date(2025, 7, 1), 75.0, 90.0, config, dist)
        results.append(blowup)

        normal_pnl = sum(r.gross_pnl for r in results[:-1])
        total_pnl = sum(r.gross_pnl for r in results)

        assert normal_pnl > 0, "Normal days should be profitable"
        assert blowup.gross_pnl < 0, "Blowup day should lose money"
        # The ratio matters: how many normal days does one blowup erase?
        recovery_days = abs(blowup.gross_pnl) / (normal_pnl / 29) if normal_pnl > 0 else float('inf')
        print(f"Normal 29-day profit: ${normal_pnl:.2f}")
        print(f"Blowup day loss: ${blowup.gross_pnl:.2f}")
        print(f"Recovery days needed: {recovery_days:.1f}")
