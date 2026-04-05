from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TokenType(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class TempSlot:
    """One temperature range within a weather event, e.g. '82°F or above'."""
    token_id_yes: str
    token_id_no: str
    outcome_label: str
    temp_lower_f: float | None  # None means open-ended (e.g. "below 60°F")
    temp_upper_f: float | None  # None means open-ended (e.g. "90°F or above")
    price_yes: float = 0.0
    price_no: float = 0.0
    spread: float | None = None  # YES/NO price spread (lower = more liquid)

    @property
    def temp_midpoint_f(self) -> float:
        if self.temp_lower_f is not None and self.temp_upper_f is not None:
            return (self.temp_lower_f + self.temp_upper_f) / 2
        if self.temp_lower_f is not None:
            return self.temp_lower_f + 1  # open upper
        if self.temp_upper_f is not None:
            return self.temp_upper_f - 1  # open lower
        return 0.0


@dataclass
class WeatherMarketEvent:
    """A Polymarket event like 'Highest temperature in NYC on April 5'."""
    event_id: str
    condition_id: str
    city: str
    market_date: date
    slots: list[TempSlot] = field(default_factory=list)
    end_timestamp: datetime | None = None
    title: str = ""
    volume: float = 0.0  # market volume in USD


@dataclass
class TradeSignal:
    """A proposed trade action."""
    token_type: TokenType  # YES or NO
    side: Side  # BUY or SELL
    slot: TempSlot
    event: WeatherMarketEvent
    expected_value: float
    estimated_win_prob: float
    suggested_size_usd: float = 0.0

    @property
    def token_id(self) -> str:
        if self.token_type == TokenType.NO:
            return self.slot.token_id_no
        return self.slot.token_id_yes

    @property
    def price(self) -> float:
        if self.token_type == TokenType.NO:
            return self.slot.price_no
        return self.slot.price_yes
