from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Forecast:
    city: str
    forecast_date: date
    predicted_high_f: float
    predicted_low_f: float
    confidence_interval_f: float  # +/- degrees (ensemble std when available)
    source: str
    fetched_at: datetime
    ensemble_spread_f: float | None = None  # inter-model disagreement
    model_count: int = 1  # number of models in ensemble


@dataclass
class Observation:
    icao: str
    temp_f: float
    observation_time: datetime
    raw_metar: str = ""
