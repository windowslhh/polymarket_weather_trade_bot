from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Forecast:
    city: str
    forecast_date: date
    predicted_high_f: float
    predicted_low_f: float
    confidence_interval_f: float  # +/- degrees
    source: str
    fetched_at: datetime


@dataclass
class Observation:
    icao: str
    temp_f: float
    observation_time: datetime
    raw_metar: str = ""
