"""Market lifecycle classification for Polymarket weather slots.

The strategy layer used to assume every slot was tradeable: a slot
showing ``price_no = 0.9995`` would be evaluated for EV and produce a
SELL signal at 0.9995 — which Polymarket then rejects because the
market is *closed*, leaving the bot in a 15-min retry-and-fail loop
that wastes API credits and clogs the alerts channel.

This module classifies a slot as one of:

  - OPEN              normal: trade as before.
  - RESOLVED_WINNER   our NO won; the *settler* should redeem, the
                      strategy layer must NOT generate any signal for it.
  - RESOLVED_LOSER    our NO lost; the position will expire worthless
                      on its own — no SELL, no exit, no trim.
  - RESOLVING         Gamma flipped ``closed=true`` but outcome prices
                      are ambiguous (mid-dispute window, lagging price
                      feed) — skip until next cycle.
  - UNKNOWN           we don't have Gamma data for this token; skip
                      defensively — better than acting on stale price
                      cache.

Detection priority (cheapest → most expensive):

  1. Gamma's ``closed`` field on the per-market dict — primary signal.
  2. Slot price at the rail (``>= 0.99``) — fallback when Gamma data is
     missing entirely (caller passes ``gamma_data=None``).  This is a
     coarse heuristic: at 0.99+ the slot is almost certainly resolved
     in our favour (we hold NO); the settler will confirm on-chain.
  3. On-chain ``payoutDenominator`` — opt-in via ``on_chain_check``.
     Reserved for the settler's *trigger* moment — not used in the hot
     path because each call is a Polygon RPC round-trip.

The classifier is a *pure function*: it does not touch the network on
the cheap path (#1, #2).  The caller decides whether to enable #3.
"""
from __future__ import annotations

import json
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class MarketState(Enum):
    OPEN = "open"
    RESOLVED_WINNER = "resolved_winner"
    RESOLVED_LOSER = "resolved_loser"
    RESOLVING = "resolving"
    UNKNOWN = "unknown"


def _coerce_outcome_prices(raw) -> list[float] | None:
    """Normalise Gamma ``outcomePrices`` (sometimes a JSON-encoded string)
    into a ``[yes, no]`` float list, or None when unparsable."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        return [float(raw[0]), float(raw[1])]
    except (TypeError, ValueError):
        return None


def classify_market(
    token_id: str,
    gamma_data: dict | None,
    slot_price_no: float,
    *,
    on_chain_check: bool = False,
) -> MarketState:
    """Decide which lifecycle bucket the slot belongs to.

    ``token_id`` is unused in the cheap path but reserved for a future
    multi-leg market where the YES/NO classification needs to know
    which side the caller holds (see ``classify_market`` callers — they
    always pass ``slot.token_id_no`` because weather bot is NO-only).

    ``gamma_data`` is the per-market dict from Gamma's ``/events``
    response (NOT the event dict).  Pass ``None`` to fall through to
    the price-only heuristic.

    ``slot_price_no`` is the cached NO price the strategy layer was
    about to act on.  Used as the rail-detection fallback.

    ``on_chain_check`` is currently unused; the parameter is reserved
    so the settler can opt into Polygon RPC confirmation without
    forcing every position-check cycle to incur the round-trip.
    """
    # Path 2: no Gamma data → use the price rail as a coarse winner heuristic.
    # Bot holds NO; price_no >= 0.99 ⇒ NO almost certainly won.  The settler
    # will re-confirm on-chain before redeeming, so a false positive here
    # only delays re-classification for one cycle — not a correctness risk.
    if gamma_data is None:
        if slot_price_no >= 0.99:
            return MarketState.RESOLVED_WINNER
        return MarketState.UNKNOWN

    # Path 1 (primary): Gamma's ``closed`` field.
    if not gamma_data.get("closed", False):
        return MarketState.OPEN

    # closed=true → look at outcomePrices to decide winner vs loser vs
    # mid-dispute.  outcome 0 = YES, outcome 1 = NO per Polymarket convention.
    outcome_prices = _coerce_outcome_prices(gamma_data.get("outcomePrices"))
    if outcome_prices is None:
        return MarketState.RESOLVING

    yes_price, no_price = outcome_prices
    if no_price >= 0.99:
        return MarketState.RESOLVED_WINNER  # bot holds NO and NO won
    if yes_price >= 0.99:
        return MarketState.RESOLVED_LOSER   # YES won, our NO is worthless
    # closed=true but neither side at the rail: dispute-window or
    # lag in the Gamma price feed.  Treat as RESOLVING and retry.
    return MarketState.RESOLVING


# Reason codes for the decision_log REJECT path.  Re-using the gates
# module's GateResult.code convention so dashboard / audit tooling can
# treat them uniformly with the existing PRICE_INVALID / EV_BELOW_GATE
# family.  See ``CLAUDE.md`` 'Decision_log REJECT sampling'.
STATE_REJECT_REASONS: dict[MarketState, str] = {
    MarketState.RESOLVED_WINNER: "MARKET_RESOLVED_WINNER_AWAIT_REDEEM",
    MarketState.RESOLVED_LOSER: "MARKET_RESOLVED_LOSER",
    MarketState.RESOLVING: "MARKET_RESOLVING_AWAIT_FINALITY",
    MarketState.UNKNOWN: "MARKET_STATE_UNKNOWN",
}
