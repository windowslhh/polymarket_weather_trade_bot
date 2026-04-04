"""Risk management: exposure limits, circuit breakers, correlation checks."""
from __future__ import annotations

import logging
import math

from src.config import AppConfig, CityConfig

logger = logging.getLogger(__name__)


def check_circuit_breaker(daily_pnl: float | None, config: AppConfig) -> bool:
    """Return True if trading should be halted due to daily loss limit.

    Args:
        daily_pnl: Today's realized P&L (negative means loss).
        config: App configuration.

    Returns:
        True if circuit breaker is triggered (should stop trading).
    """
    if daily_pnl is None:
        return False
    if daily_pnl < -config.strategy.daily_loss_limit_usd:
        logger.warning(
            "CIRCUIT BREAKER: daily P&L $%.2f exceeds limit -$%.2f",
            daily_pnl, config.strategy.daily_loss_limit_usd,
        )
        return True
    return False


def check_exposure_limits(
    proposed_size_usd: float,
    city_exposure_usd: float,
    total_exposure_usd: float,
    config: AppConfig,
) -> bool:
    """Return True if the proposed trade would breach exposure limits.

    Args:
        proposed_size_usd: Size of the proposed trade.
        city_exposure_usd: Current exposure for this city.
        total_exposure_usd: Current total exposure.
        config: App configuration.

    Returns:
        True if limits would be breached (should NOT trade).
    """
    if city_exposure_usd + proposed_size_usd > config.strategy.max_exposure_per_city_usd:
        logger.debug("City exposure limit would be breached")
        return True
    if total_exposure_usd + proposed_size_usd > config.strategy.max_total_exposure_usd:
        logger.debug("Total exposure limit would be breached")
        return True
    return False


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def check_geographic_correlation(
    cities_with_positions: list[str],
    all_cities: list[CityConfig],
    distance_threshold_km: float = 500.0,
) -> list[tuple[str, str, float]]:
    """Check for geographically close city pairs that may be weather-correlated.

    Returns list of (city1, city2, distance_km) for pairs closer than threshold.
    """
    city_map = {c.name: c for c in all_cities}
    warnings: list[tuple[str, str, float]] = []

    active = [c for c in cities_with_positions if c in city_map]
    for i, c1 in enumerate(active):
        for c2 in active[i + 1:]:
            cfg1, cfg2 = city_map[c1], city_map[c2]
            dist = _haversine_km(cfg1.lat, cfg1.lon, cfg2.lat, cfg2.lon)
            if dist < distance_threshold_km:
                warnings.append((c1, c2, dist))
                logger.warning(
                    "Geographic correlation: %s and %s are %.0f km apart",
                    c1, c2, dist,
                )

    return warnings
