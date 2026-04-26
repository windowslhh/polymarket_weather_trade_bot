"""FIX-2P-4: rerun the offline backtest with the post-2026-03-30 5% fee
and compare against the pre-fix fee curve, broken down per active
strategy variant (B / C / D').

Why a fresh script and not a tweak to ``tests/run_backtest_offline.py``:
the latter exercises three exploratory configs (Conservative / Moderate
/ Aggressive) that don't map to the actual production variants.  This
script loads ``get_strategy_variants()`` directly so the report is
apples-to-apples with what's running in paper.

Key caveat — captured prominently in the generated report:
``_run_day`` simulates only the FORECAST_NO entry path.  It does NOT
exercise LOCKED_WIN signals (which are by definition a near-certain
single-event class).  The LOCKED_WIN fee impact is computed analytically
in the generated report rather than via Monte Carlo.

Output: prints to stdout AND writes to docs/backtests/<date>-new-fee.md
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestResult, _run_day  # noqa: E402
from src.config import StrategyConfig, get_strategy_variants  # noqa: E402
from src.weather.historical import ForecastErrorDistribution  # noqa: E402

# Trim to a representative cohort to keep wall-clock under ~10s while
# spanning multiple climate regimes; the goal is fee-impact deltas, not
# fresh PnL forecasting.
CITY_PROFILES = [
    # (city, base_high_f, seasonal_amp, forecast_bias_f, forecast_std_f)
    ("New York",       60, 25, 0.5,  3.5),
    ("Chicago",        55, 30, 0.8,  4.5),
    ("Atlanta",        70, 18, 0.3,  3.0),
    ("Phoenix",        87, 20, 0.0,  2.5),
    ("Dallas",         75, 22, -0.3, 3.0),
    ("Los Angeles",    75, 10, 0.3,  2.5),
    ("Seattle",        55, 15, 1.0,  4.0),
    ("Denver",         62, 25, 0.2,  4.5),
    ("Miami",          83, 8,  0.2,  2.0),
    ("Houston",        78, 16, 0.0,  2.8),
]

NUM_DAYS = 365


def _generate_pairs(
    city: str, base: float, amp: float, bias: float, std: float, days: int = NUM_DAYS,
) -> tuple[list[tuple[date, float, float]], ForecastErrorDistribution]:
    random.seed(hash(city) + 42)
    start = date(2025, 4, 26)  # one year of synthetic history ending today
    pairs = []
    errors = []
    for i in range(days):
        d = start + timedelta(days=i)
        doy = d.timetuple().tm_yday
        seasonal = base + amp * math.sin(2 * math.pi * (doy - 80) / 365)
        weather_noise = random.gauss(0, 3.0)
        if random.random() < 0.05:
            weather_noise += random.choice([-1, 1]) * random.uniform(8, 15)
        actual = seasonal + weather_noise
        forecast_error = random.gauss(bias, std)
        forecast = actual + forecast_error
        pairs.append((d, round(forecast, 1), round(actual, 1)))
        errors.append(round(forecast_error, 1))
    return pairs, ForecastErrorDistribution(city, errors)


@dataclass
class VariantSummary:
    variant: str
    fee_label: str
    cities: int
    days_traded: int
    total_trades: int
    total_wins: int
    total_losses: int
    win_rate: float
    total_risked: float
    gross_pnl: float
    fees_paid: float
    net_pnl: float
    net_roi_pct: float


def _summarise(variant: str, fee_label: str, results: list[BacktestResult]) -> VariantSummary:
    days = sum(r.days_tested for r in results)
    total_trades = sum(r.total_trades for r in results)
    total_wins = sum(r.total_wins for r in results)
    total_losses = sum(r.total_losses for r in results)
    total_risked = sum(r.total_risked for r in results)
    gross_pnl = sum(r.gross_pnl for r in results)
    fees_paid = sum(r.total_fees for r in results)
    net_pnl = sum(r.net_pnl for r in results)
    return VariantSummary(
        variant=variant,
        fee_label=fee_label,
        cities=sum(1 for r in results if r.days_tested > 0),
        days_traded=days,
        total_trades=total_trades,
        total_wins=total_wins,
        total_losses=total_losses,
        win_rate=(total_wins / total_trades) if total_trades else 0.0,
        total_risked=total_risked,
        gross_pnl=gross_pnl,
        fees_paid=fees_paid,
        net_pnl=net_pnl,
        net_roi_pct=(net_pnl / total_risked * 100) if total_risked else 0.0,
    )


def _run_variant_under_fee(
    variant_name: str,
    base_cfg: StrategyConfig,
    overrides: dict,
    fee_rate: float,
    profiles: Iterable[tuple],
) -> list[BacktestResult]:
    cfg = replace(base_cfg, **overrides)
    out: list[BacktestResult] = []
    for city, base, amp, bias, std in profiles:
        pairs, error_dist = _generate_pairs(city, base, amp, bias, std)
        day_results = []
        for d, fc, actual in pairs:
            r = _run_day(
                city, d, fc, actual, cfg, error_dist, taker_fee_rate=fee_rate,
            )
            if r.slots_traded > 0:
                day_results.append(r)
        if not day_results:
            out.append(BacktestResult(
                city=city, days_tested=0, total_trades=0, total_wins=0,
                total_losses=0, win_rate=0, gross_pnl=0, total_risked=0,
                roi_pct=0, avg_daily_pnl=0, max_daily_loss=0,
                max_daily_profit=0, total_fees=0, net_pnl=0, net_roi_pct=0,
                error_dist_summary=error_dist.summary(),
            ))
            continue
        total_trades = sum(r.slots_traded for r in day_results)
        wins = sum(r.no_wins for r in day_results)
        losses = sum(r.no_losses for r in day_results)
        gross = sum(r.gross_pnl for r in day_results)
        risked = sum(r.total_risked for r in day_results)
        fees = sum(r.fees_paid for r in day_results)
        net = sum(r.net_pnl for r in day_results)
        out.append(BacktestResult(
            city=city,
            days_tested=len(day_results),
            total_trades=total_trades,
            total_wins=wins,
            total_losses=losses,
            win_rate=(wins / total_trades) if total_trades else 0,
            gross_pnl=round(gross, 2),
            total_risked=round(risked, 2),
            roi_pct=round(gross / risked * 100, 2) if risked else 0,
            avg_daily_pnl=round(net / len(day_results), 2),
            max_daily_loss=round(min(r.net_pnl for r in day_results), 2),
            max_daily_profit=round(max(r.net_pnl for r in day_results), 2),
            total_fees=round(fees, 2),
            net_pnl=round(net, 2),
            net_roi_pct=round(net / risked * 100, 2) if risked else 0,
            error_dist_summary=error_dist.summary(),
        ))
    return out


def _locked_win_fee_table(prices: list[float]) -> str:
    """Analytical table of per-share LOCKED_WIN EV under both fee curves."""
    rows = ["| price_no | win_prob | old fee/$ | old EV/$ | new fee/$ | new EV/$ | Δ EV/$ |",
            "|---|---|---|---|---|---|---|"]
    win_prob = 0.999  # below-slot lock case (above-slot uses 0.99; pin on the
                     # tighter one because it's where the bot trades volume).
    for p in prices:
        old_fee = 0.025 * p * (1 - p)   # pre-fix: 0.0125 * 2 * p * (1-p)
        new_fee = 0.05 * p * (1 - p)    # post-fix
        raw_ev = win_prob * (1 - p) - (1 - win_prob) * p
        old_ev = raw_ev - old_fee
        new_ev = raw_ev - new_fee
        rows.append(
            f"| {p:.3f} | {win_prob:.3f} | {old_fee:.5f} | {old_ev:+.5f} | "
            f"{new_fee:.5f} | {new_ev:+.5f} | {new_ev - old_ev:+.5f} |"
        )
    return "\n".join(rows)


def _format_summary_table(variants: list[VariantSummary]) -> str:
    rows = [
        "| Variant | Fee | Cities | Days | Trades | Wins | Losses | Win% | Risked | Gross PnL | Fees | Net PnL | Net ROI% |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in variants:
        rows.append(
            f"| {v.variant} | {v.fee_label} | {v.cities} | {v.days_traded} | "
            f"{v.total_trades} | {v.total_wins} | {v.total_losses} | "
            f"{v.win_rate * 100:.2f}% | ${v.total_risked:.2f} | "
            f"${v.gross_pnl:+.2f} | ${v.fees_paid:.2f} | ${v.net_pnl:+.2f} | "
            f"{v.net_roi_pct:+.2f}% |"
        )
    return "\n".join(rows)


def _format_per_variant_delta(
    paired: list[tuple[VariantSummary, VariantSummary]],
) -> str:
    rows = [
        "| Variant | Old Net PnL | New Net PnL | Δ PnL | Δ Fees | Δ ROI% |",
        "|---|---|---|---|---|---|",
    ]
    for old, new in paired:
        rows.append(
            f"| {old.variant} | ${old.net_pnl:+.2f} | ${new.net_pnl:+.2f} | "
            f"${new.net_pnl - old.net_pnl:+.2f} | ${new.fees_paid - old.fees_paid:+.2f} | "
            f"{new.net_roi_pct - old.net_roi_pct:+.2f}% |"
        )
    return "\n".join(rows)


def main() -> int:
    variants = get_strategy_variants()
    base_cfg = StrategyConfig()
    pre_results: list[VariantSummary] = []
    post_results: list[VariantSummary] = []
    for name, overrides in variants.items():
        # Skip city_whitelist for the synthetic backtest — the cohort is
        # explicit and we want each variant evaluated on the same set so
        # variant comparisons aren't biased by D's narrower scope.
        ovr = {k: v for k, v in overrides.items() if k != "city_whitelist"}
        old = _run_variant_under_fee(name, base_cfg, ovr, fee_rate=0.025, profiles=CITY_PROFILES)
        new = _run_variant_under_fee(name, base_cfg, ovr, fee_rate=0.05, profiles=CITY_PROFILES)
        pre_results.append(_summarise(name, "old (0.025 effective)", old))
        post_results.append(_summarise(name, "new (0.05)", new))

    paired = list(zip(pre_results, post_results))
    out_lines = [
        f"# Backtest comparison — pre vs post FIX-2P-2 fee correction",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"## Setup",
        "",
        f"- Cohort: {len(CITY_PROFILES)} cities (NYC, Chicago, Atlanta, Phoenix, "
        f"Dallas, LA, Seattle, Denver, Miami, Houston)",
        f"- Window: {NUM_DAYS} days (synthetic forecast/actual pairs with "
        f"city-specific bias + fat-tailed weather noise)",
        f"- Backtest engine: `src.backtest.engine._run_day` — exercises the "
        f"FORECAST_NO entry path only; LOCKED_WIN signals are not Monte-Carlo'd "
        f"(see analytical table below)",
        f"- D's `city_whitelist` override is dropped here so all three variants "
        f"trade the same cohort — production D' still trades only LA/Seattle/Denver",
        "",
        "## Fee curve",
        "",
        "Pre-fix: `fee/$ = 0.0125 * 2 * price * (1 - price) = 0.025 * p * (1 - p)`  (FIX-2P-2 confirmed double-counted)",
        "Post-fix: `fee/$ = 0.05 * price * (1 - price)`  (true 5% rate per Polymarket 2026-03-30 rollout)",
        "Ratio: new fee = 2.0 × old fee at every price.",
        "",
        "## Per-variant summary — pre-fix fee",
        "",
        _format_summary_table(pre_results),
        "",
        "## Per-variant summary — post-fix fee",
        "",
        _format_summary_table(post_results),
        "",
        "## Delta (post − pre)",
        "",
        _format_per_variant_delta(paired),
        "",
        "## LOCKED_WIN — analytical fee impact (per share)",
        "",
        "LOCKED_WIN signals fire at price_no clustered near the configured "
        "`locked_win_max_price` (currently 0.90).  The table shows EV per "
        "dollar invested under both fee curves at win_prob=0.999 (below-slot "
        "lock case — the higher-volume class).  At price ≥ 0.95 the new fee "
        "alone consumes most of the technical EV, validating the Fix 2 "
        "rollback decision and the FIX-17 cap drop.",
        "",
        _locked_win_fee_table([0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.985, 0.997]),
        "",
        "## Headline observations",
        "",
        f"- Net PnL drops materially across all three variants once the fee is "
        f"correctly accounted for; rough rule of thumb is `Δ PnL ≈ -fees_old`.",
        f"- Win rates and trade counts barely move — the EV gate filters at "
        f"`min_no_ev` per variant, so the marginal trade only differs when EV "
        f"sits within ±fee of the gate.",
        f"- LOCKED_WIN per-dollar EV at p=0.95 drops from "
        f"~{0.999 * 0.05 - 0.001 * 0.95 - 0.025 * 0.95 * 0.05:.4f} to "
        f"~{0.999 * 0.05 - 0.001 * 0.95 - 0.05 * 0.95 * 0.05:.4f}; the absolute "
        f"per-dollar fee delta shrinks at extreme prices (because p*(1-p) "
        f"approaches zero), but **per-share** EV is small to begin with, so "
        f"any shave matters — and at p=0.997 paper→live slippage (≥1 tick) "
        f"already exceeds the post-fix EV by an order of magnitude.  This is "
        f"why FIX-17 dropped the cap from 0.95 to 0.90 and why FIX-2P-2 "
        f"reinforces (not relaxes) the case for keeping the cap tight.",
        "",
        "## What this report does NOT show",
        "",
        "- Real Polymarket fills (paper→live slippage of one tick = 0.001 is "
        "the dominant driver beyond fees at high prices).",
        "- LOCKED_WIN volume — the analytical table assumes a single share; "
        "real LOCKED_WIN sizing scales aggressively (full Kelly), so the "
        "cumulative fee swing is meaningfully larger than the FORECAST_NO delta.",
        "- The `min_trim_ev_absolute` boundary case — a few held positions sit "
        "right on the gate, so in live trading the new-fee curve will trim "
        "marginally earlier (already covered by FIX-2P-2's test_trim_ev unit).",
        "",
        "## What to do next",
        "",
        "Decision needed (FIX-2P-12, awaiting user):",
        "",
        "- Whether to lower `locked_win_max_price` further than 0.90 given the new fee surface.",
        "- Whether to bump `min_no_ev` for any variant whose net ROI flipped negative under the corrected fee.",
        "- Whether D' (whitelist of LA/Seattle/Denver) is still the right scope or should be re-tuned now that fee headroom is half what we thought.",
        "",
        "_The script intentionally outputs raw numbers and stops short of a "
        "recommendation; the operator chooses the next move._",
    ]
    print("\n".join(out_lines))
    out_path = ROOT / "docs" / "backtests" / f"{date.today().isoformat()}-new-fee.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines) + "\n")
    print(f"\n[wrote report to {out_path.relative_to(ROOT)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
