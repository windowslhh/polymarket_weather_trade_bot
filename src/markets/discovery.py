"""Discover active weather temperature markets on Polymarket via Gamma API."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import json

import httpx

from src.config import CityConfig
from src.markets.models import TempSlot, WeatherMarketEvent
from src.markets.resolution import parse_resolution_from_event

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Pattern to extract temperature from outcome labels like "82°F or above", "78°F to 81°F"
_TEMP_RANGE_RE = re.compile(
    r"(?P<lower>\d+)\s*°?\s*F?\s*(?:to|-)\s*(?P<upper>\d+)\s*°?\s*F?",
    re.IGNORECASE,
)
_TEMP_ABOVE_RE = re.compile(r"(?P<temp>\d+)\s*°?\s*F?\s*or\s*(?:above|higher|more)", re.IGNORECASE)
_TEMP_BELOW_RE = re.compile(r"(?:(?:below|under|less\s*than)\s*(?P<temp>\d+)\s*°?\s*F|(?P<temp2>\d+)\s*°?\s*F?\s*or\s*(?:below|lower|less))", re.IGNORECASE)
_TEMP_SINGLE_RE = re.compile(r"^(?P<temp>\d+)\s*°?\s*F$", re.IGNORECASE)

# Pattern to extract city name and date from event title
_TITLE_PATTERN = re.compile(
    r"(?:highest|high)\s+temperature\s+in\s+(?P<city>.+?)\s+on\s+(?P<date>.+?)\??\s*$",
    re.IGNORECASE,
)


def _parse_temp_bounds(label: str) -> tuple[float | None, float | None]:
    """Parse temperature lower/upper bounds from an outcome label."""
    m = _TEMP_RANGE_RE.search(label)
    if m:
        return float(m.group("lower")), float(m.group("upper"))

    m = _TEMP_ABOVE_RE.search(label)
    if m:
        return float(m.group("temp")), None

    m = _TEMP_BELOW_RE.search(label)
    if m:
        temp_val = m.group("temp") or m.group("temp2")
        return None, float(temp_val)

    m = _TEMP_SINGLE_RE.search(label.strip())
    if m:
        t = float(m.group("temp"))
        return t, t

    return None, None


def _parse_date(date_str: str) -> date | None:
    """Try to parse a date string like 'April 5' or '2026-04-05'."""
    for fmt in ("%B %d", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=date.today().year)
            return dt.date()
        except ValueError:
            continue
    return None


def _match_city(event_city: str, configured_cities: list[CityConfig]) -> CityConfig | None:
    """Match an event's city name to a configured city.

    Prefers exact (case-insensitive) match over substring match to avoid
    ambiguity between cities that share a name prefix (e.g. "Portland" could
    match both "Portland OR" and "Portland ME").  Falls back to substring
    matching only when no exact match exists.
    """
    event_lower = event_city.lower().strip()
    # Pass 1: exact match
    for city in configured_cities:
        if city.name.lower() == event_lower:
            return city
    # Pass 2: substring match (event name contains city name or vice versa)
    substring_matches = [
        city for city in configured_cities
        if city.name.lower() in event_lower or event_lower in city.name.lower()
    ]
    if len(substring_matches) == 1:
        return substring_matches[0]
    if len(substring_matches) > 1:
        # Multiple substring matches — log and return None to avoid wrong assignment
        logger.warning(
            "Ambiguous city match for %r: candidates %s — skipping",
            event_city, [c.name for c in substring_matches],
        )
        return None
    return None


def _compute_settle_timestamp(
    markets: list[dict],
    market_date: date,
    city_tz: str | None,
) -> datetime | None:
    """Compute the true settlement timestamp for a weather event.

    Polymarket's ``event.endDate`` is a 12:00 UTC placeholder shared across
    all markets resolving on the same calendar date — it is not the actual
    settle moment.  The real settle for a "Highest temperature in <city> on
    <date>" market is the end of that calendar day in the city's local
    timezone (≡ 00:00 of the next day, anchored in ``city_tz``).

    Both resolution paths construct the answer via ``ZoneInfo(city_tz)``-
    anchored midnight of ``date + 1`` and convert to UTC.  This makes the
    result correct on DST transition days, where a city's calendar day is
    23h or 25h long; a literal ``+24h`` on the UTC-shifted ``gameStartTime``
    would be wrong by ±1h on those two days per year.

    Resolution order:

      1. ``markets[0].gameStartTime`` (when present) — parse to UTC, convert
         back to ``city_tz``, and take ``date()``.  This yields the city-local
         calendar date that Polymarket itself asserts the market belongs to,
         which is more authoritative than the title-parsed ``market_date``.
         Note: ``gameStartTime ≈ city-local midnight`` is an *empirical*
         observation about Gamma's payload, not a contractual guarantee —
         that's why the fallback is retained even after this check succeeds.
      2. Fallback: ``market_date`` (parsed from the event title).
      3. ``None`` if ``city_tz`` is missing or unrecognised — without a
         timezone we cannot construct a meaningful settle moment.
    """
    if not city_tz:
        return None
    try:
        tz = ZoneInfo(city_tz)
    except ZoneInfoNotFoundError:
        return None

    # Prefer the date asserted by Polymarket via gameStartTime; otherwise
    # fall back to the date parsed from the event title.
    settle_date: date | None = None
    gst_raw = markets[0].get("gameStartTime") if markets else None
    if gst_raw:
        try:
            # Gamma returns formats observed in production:
            #   "2026-04-16 04:00:00+00"  (space + offset without colon)
            #   "2026-04-16T04:00:00Z"
            #   "2026-04-16T04:00:00+00:00"
            s = str(gst_raw).strip().replace(" ", "T")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            elif s.endswith("+00"):
                s = s + ":00"
            gst_dt = datetime.fromisoformat(s)
            if gst_dt.tzinfo is None:
                gst_dt = gst_dt.replace(tzinfo=timezone.utc)
            settle_date = gst_dt.astimezone(tz).date()
        except (ValueError, TypeError) as exc:
            logger.debug("Failed to parse gameStartTime %r: %s", gst_raw, exc)
            settle_date = None

    if settle_date is None:
        settle_date = market_date

    local_next_midnight = datetime.combine(
        settle_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=tz,
    )
    return local_next_midnight.astimezone(timezone.utc)


async def discover_weather_markets(
    cities: list[CityConfig],
    client: httpx.AsyncClient | None = None,
    min_volume: float = 0.0,
    max_spread: float = 1.0,
    max_days_ahead: int = 7,
) -> list[WeatherMarketEvent]:
    """Scan Gamma API for active weather temperature markets matching configured cities."""
    should_close = client is None
    client = client or httpx.AsyncClient(timeout=30)

    events: list[WeatherMarketEvent] = []
    try:
        offset = 0
        limit = 100
        while True:
            resp = await client.get(
                f"{GAMMA_API_URL}/events",
                params={
                    "tag_slug": "weather",
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break

            for event_data in data:
                title = event_data.get("title", "")
                m = _TITLE_PATTERN.search(title)
                if not m:
                    continue

                city_name = m.group("city")
                date_str = m.group("date")
                city_cfg = _match_city(city_name, cities)
                if not city_cfg:
                    continue

                market_date = _parse_date(date_str)
                if not market_date:
                    continue

                # Skip past/future markets — compare against city-local date, not UTC.
                # During UTC midnight (00:00–06:00 UTC), US west coast cities are still
                # on the previous day; date.today() (UTC) would wrongly filter out
                # same-day markets for cities like Los Angeles (PDT/PST = UTC-7/8).
                try:
                    city_tz = ZoneInfo(city_cfg.tz) if city_cfg.tz else timezone.utc
                    local_today = datetime.now(city_tz).date()
                except Exception:
                    local_today = datetime.now(timezone.utc).date()

                if market_date < local_today:
                    continue
                if market_date > local_today + timedelta(days=max_days_ahead):
                    continue

                # Parse temperature slots from child markets
                markets = event_data.get("markets", [])
                slots: list[TempSlot] = []
                for mkt in markets:
                    outcomes = mkt.get("outcomes", [])
                    outcome_prices = mkt.get("outcomePrices", [])
                    tokens = mkt.get("clobTokenIds", [])

                    # Gamma API returns these as JSON strings sometimes
                    if isinstance(outcomes, str):
                        try: outcomes = json.loads(outcomes)
                        except (json.JSONDecodeError, TypeError): outcomes = []
                    if isinstance(outcome_prices, str):
                        try: outcome_prices = json.loads(outcome_prices)
                        except (json.JSONDecodeError, TypeError): outcome_prices = []
                    if isinstance(tokens, str):
                        try: tokens = json.loads(tokens)
                        except (json.JSONDecodeError, TypeError): tokens = []

                    if len(outcomes) < 2 or len(tokens) < 2:
                        continue

                    # outcomes[0] = YES label, outcomes[1] = NO label typically
                    label = mkt.get("question", "") or (outcomes[0] if outcomes else "")
                    lower, upper = _parse_temp_bounds(label)
                    if lower is None and upper is None:
                        continue

                    prices = []
                    for p in outcome_prices:
                        try:
                            prices.append(float(p))
                        except (ValueError, TypeError):
                            prices.append(0.0)

                    price_yes = prices[0] if prices else 0.0
                    price_no = prices[1] if len(prices) > 1 else 0.0
                    slot_spread = abs(1.0 - price_yes - price_no) if price_yes > 0 and price_no > 0 else None

                    # Skip illiquid slots
                    if slot_spread is not None and slot_spread > max_spread:
                        logger.debug("Skipping illiquid slot %s (spread=%.3f)", label, slot_spread)
                        continue

                    slots.append(TempSlot(
                        token_id_yes=tokens[0] if tokens else "",
                        token_id_no=tokens[1] if len(tokens) > 1 else "",
                        outcome_label=label,
                        temp_lower_f=lower,
                        temp_upper_f=upper,
                        price_yes=price_yes,
                        price_no=price_no,
                        spread=slot_spread,
                    ))

                if not slots:
                    continue

                # Parse volume and filter low-liquidity markets
                try:
                    event_volume = float(event_data.get("volume", 0) or 0)
                except (ValueError, TypeError):
                    event_volume = 0.0

                if min_volume > 0 and event_volume < min_volume:
                    logger.debug("Skipping low-volume market %s (vol=$%.0f)", city_cfg.name, event_volume)
                    continue

                # Compute true settlement timestamp.
                # Polymarket's event.endDate is a 12:00 UTC placeholder shared by
                # all markets settling on the same calendar date — it is NOT the
                # actual resolution moment. The real settle is end-of-day in the
                # city's local timezone (e.g. NYC Apr 16 → 2026-04-17 04:00 UTC).
                # Prefer markets[0].gameStartTime + 24h; fall back to city-tz
                # midnight of (market_date + 1).  See docs/fixes/2026-04-16-settlement-time.md
                end_dt = _compute_settle_timestamp(markets, market_date, city_cfg.tz)

                # Parse resolution source from event description
                resolution = parse_resolution_from_event(event_data, city_cfg.name)

                events.append(WeatherMarketEvent(
                    event_id=event_data.get("id", ""),
                    condition_id=event_data.get("conditionId", ""),
                    city=city_cfg.name,
                    market_date=market_date,
                    slots=slots,
                    end_timestamp=end_dt,
                    title=title,
                    volume=event_volume,
                    resolution_source=resolution,
                ))

            if len(data) < limit:
                break
            offset += limit

        logger.info("Discovered %d weather market events across configured cities", len(events))
    except Exception:
        logger.exception("Failed to discover weather markets")
    finally:
        if should_close:
            await client.aclose()

    return events
