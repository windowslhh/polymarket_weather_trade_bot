"""Async wrapper around py-clob-client for Polymarket CLOB API."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from src.config import AppConfig

logger = logging.getLogger(__name__)

# v2-8 (2026-04-27): py-clob-client-v2 ships ``_http_client = httpx.Client(
# http2=True)`` at module level with no explicit timeout — httpx's default
# 5s pool/read timeout combined with HTTP/2 keep-alive on macOS triggered
# spurious ReadTimeouts on the first live cycle (CLOB midpoint endpoint
# itself responded in 3-4s, verified by curl from the same mac).  One
# stuck call propagated up to the rebalancer and froze the cycle.  Force
# HTTP/1.1 and a 30s read timeout so transient slowness no longer blocks.
# Idempotent — runs at import time before _get_client lazy-imports the
# SDK, so every CLOB call goes through the patched client.
try:
    import httpx as _httpx
    import py_clob_client_v2.http_helpers.helpers as _v2_http_helpers
    _v2_http_helpers._http_client = _httpx.Client(
        http2=False, timeout=_httpx.Timeout(30.0),
    )
except Exception as exc:  # noqa: BLE001
    logger.warning("v2-8 httpx monkey-patch failed: %s", exc)

# v2-9 (2026-04-29): tighten maker_amount precision to 2 decimals.
# Polymarket's CLOB now 400s BUY orders whose maker_amount carries
# more than 2 fractional digits — observed message:
#   "invalid amounts, the market buy orders maker amount supports a
#    max accuracy of 2 decimals, taker amount a max of 4 decimals"
# (server rule tightened post-2026-04-28 cutover; previously the
# server was lenient and the SDK's local rounding silently fit).
# The SDK's ``ROUNDING_CONFIG["0.01"]`` ships ``amount=4`` — for
# 0.01-tick markets ``round_down(size,2) * round_normal(price,2)``
# can produce up to 4 decimals (e.g. 7.41 × 0.55 → 4.0755), which
# the SDK ships as-is and the gateway rejects.  Forcing
# ``amount=2`` for every tick size means the SDK's own
# ``round_down(raw_amt, amount)`` step (already in
# ``get_order_amounts``) clamps maker_amount to cents BEFORE the
# wire payload is built.  SELL is also affected via the same
# ``round_config.amount`` field, but SELL's taker_amount (USDC) is
# capped server-side at 4 decimals — clamping to 2 is over-strict
# but still server-valid; the per-order USDC delta is ≤ $0.005,
# below our $1 SELL gate so functionally invisible.  Idempotent at
# import time, runs before any ``_get_client`` lazy-init.
#
# 2026-05-01 (review #5): switch from ``logger.warning + continue`` to
# ``sys.exit(2)`` analogous to ``check_station_alignment``.  Rationale:
# the original try/except quietly let the bot start with the wire
# payload still carrying 4-5 decimal amounts, which Polymarket then
# rejected mid-cycle.  The 2026-04-29 production incident (20 BUY
# rejections over 7h, audit confirmed all pre-deploy) had nothing to
# do with the patch itself — the patch worked once deployed — but the
# silent fail-soft hides the same class of regression if py-clob-client
# ships a future version where ``ROUNDING_CONFIG`` / ``RoundConfig``
# moves or renames.  Fail-fast at startup makes "the SDK shape changed"
# loud instead of "trades silently fail at 4 decimals again."  Paper /
# dry-run modes go through the same import path so they fail-fast too;
# this is intentional — paper config divergence from live is exactly
# what we're protecting against.
try:
    from py_clob_client_v2.order_builder import builder as _v2_builder
    from py_clob_client_v2.order_builder.builder import RoundConfig as _RC
    for _ts, _cfg in list(_v2_builder.ROUNDING_CONFIG.items()):
        _v2_builder.ROUNDING_CONFIG[_ts] = _RC(
            price=_cfg.price, size=_cfg.size, amount=2,
        )
    # Belt-and-braces: verify the in-memory dict actually carries
    # ``amount=2`` after the rewrite.  If a future SDK adds new tick
    # sizes the loop won't have populated, this catches the gap before
    # the first live order tries to use that tick.
    for _ts, _cfg in _v2_builder.ROUNDING_CONFIG.items():
        if _cfg.amount != 2:
            raise RuntimeError(
                f"ROUNDING_CONFIG[{_ts!r}].amount={_cfg.amount}, "
                f"expected 2 — patch did not take effect"
            )
except Exception as exc:  # noqa: BLE001
    import sys as _sys
    logger.error(
        "v2-9 ROUNDING_CONFIG monkey-patch failed: %s — refusing to "
        "start.  Polymarket will reject orders with >2 decimal "
        "maker_amount; failing fast is safer than 400'ing every BUY "
        "until someone notices.  Likely cause: py-clob-client SDK "
        "shape changed (RoundConfig fields renamed, ROUNDING_CONFIG "
        "moved, etc.) — inspect "
        "py_clob_client_v2.order_builder.builder and re-pin the "
        "patch.  Bypass with care.",
        exc,
    )
    _sys.exit(2)

# FIX-04: network resilience knobs. Kept module-level so tests can monkeypatch
# them without touching the client.
ORDER_TIMEOUT_S = 30.0
ORDER_MAX_ATTEMPTS = 3
# 429 rate-limit: sleep min(2**n + jitter, cap). Polymarket doesn't publish a
# Retry-After contract on the CLOB, so we rely on exponential-with-jitter and
# cap to keep the bot responsive rather than stalling for minutes.
RATE_LIMIT_CAP_S = 30.0

# Review Blocker #1 (2026-04-24): timeouts must NOT retry in-cycle.
# `asyncio.timeout(N)` cancels the awaiting task but cannot cancel the
# underlying synchronous HTTP POST running in the asyncio.to_thread worker.
# If we retry, the CLOB may end up creating TWO orders — our
# idempotency_key is a client-side breadcrumb only, py-clob-client does
# NOT forward it to Polymarket.  We short-circuit to success=False instead
# and let the startup reconciler (FIX-05) reconcile against CLOB on the
# next bot start.
TIMEOUT_RETRIES = False

# Polymarket CLOB tick size for weather markets (0.01 USDC).  Used by the
# FAK cross-spread fix to compute ``best_ask + 1 tick`` / ``best_bid - 1 tick``
# and as the lower-bound on submitted limit prices.  Promoted from a local
# constant in ``place_limit_order`` to module level so the cold-start
# ``price <= TICK`` guard at the entry of ``place_limit_order`` and the
# cross-the-spread block lower in the method share a single source.
TICK = 0.01


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort heuristic: py-clob-client wraps requests, so 429s arrive as
    generic exceptions whose stringified form contains '429' or 'rate limit'.
    """
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "rate_limit" in msg or "too many" in msg


def _is_order_version_mismatch_error(exc: BaseException) -> bool:
    """Polymarket exchange-cutover signal — see ``_force_refresh_clob_version``."""
    return "order_version_mismatch" in str(exc)


def _force_refresh_clob_version(client) -> bool:
    """Flush the SDK's in-memory ``__cached_version`` so the *next* order is
    built against the server's current order-schema version.

    Why this exists: ``py_clob_client_v2`` 1.0.0 caches the value of
    ``GET /version`` on first call and never re-reads it.  It *does* ship a
    refresh path — ``post_order`` flips ``__resolve_version(force_update=True)``
    on a ``order_version_mismatch`` *response dict* — but the helper at
    ``http_helpers/helpers.py:78`` raises ``PolyApiException`` on any non-200,
    so that branch is dead code in practice.  Result: a bot whose first
    ``/version`` call landed in the 2026-04-28 cancel-only window cached
    ``1`` and signed V1 orders against a V2-only server forever (HTTP 400
    ``order_version_mismatch``) until human-restart.

    Belt-and-suspenders: nullify the name-mangled cache attribute *first*,
    then call ``__resolve_version(force_update=True)``.  If a future SDK
    refactor renames or breaks the ``force_update`` branch, the explicit
    ``= None`` ensures the next ``__resolve_version()`` call still re-fetches
    via the cache-empty path.  Both writes use the SDK's name-mangled
    private symbols (``_ClobClient__cached_version``,
    ``_ClobClient__resolve_version``); changes to those names will trip
    the broad ``except`` and surface a warning instead of silent stale-cache
    behavior.

    Returns True iff the cache was successfully flipped.
    """
    try:
        client._ClobClient__cached_version = None
        client._ClobClient__resolve_version(force_update=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "force-refresh CLOB version cache failed: %s — next order will "
            "re-use stale cache and likely fail again",
            exc,
        )
        return False


@dataclass
class OrderResult:
    order_id: str
    success: bool
    message: str = ""


@dataclass
class Position:
    token_id: str
    size: float
    avg_price: float
    side: str  # "BUY"


@dataclass
class FillSummary:
    """Aggregated fill data for a single order, derived from ``get_trades``.

    ``match_price`` is the effective per-share entry (USDC paid / shares
    received), so the dashboard's "entry" column reflects the slippage-adjusted
    cost rather than the limit price the bot submitted.  ``fee_paid_usd`` is
    the per-share fee × shares matched, taker-side only.

    Bug C (2026-04-29): ``net_shares`` is the on-chain ERC1155 balance the bot
    actually received after the Polymarket BUY taker fee was deducted from
    the token side.  Equals ``trade.size`` when ``fee_rate_bps == 0``, else
    ``trade.size × (1 − taker_rate × (1 − price))`` per matched trade, summed.
    Use this to populate ``positions.shares`` so DB matches chain (the prior
    formula ``size_usd / limit_price`` drifted by both slippage and fee).
    """
    shares: float
    match_price: float
    fee_paid_usd: float
    net_shares: float


class ClobClient:
    """Async wrapper for Polymarket CLOB operations.

    Uses py-clob-client under the hood, wrapping sync calls with asyncio.to_thread.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = None

    def _get_client(self):
        """Lazy-init the py-clob-client.

        Live-mode only — paper / dry-run paths short-circuit before
        reaching this method (no real CLOB credentials required to
        simulate fills).  Two responsibilities:

        1. Pick signature mode + funder.  signature_type (per
           py-clob-client/rfq_types.py docstring):
              0 = EOA              (direct on-chain wallet)
              1 = POLY_PROXY       (Magic / email — legacy)
              2 = POLY_GNOSIS_SAFE (polymarket.com web user proxy wallet)
           If FUNDER_ADDRESS is set we are operating against a Gnosis
           Safe (the polymarket.com signup default), so signature_type=2
           and funder=address.  Without a funder we sign as the EOA
           directly (signature_type=0, no funder kwarg).

        2. Get L2 API creds.  Either the operator pre-provisioned them
           in .env (back-compat) or we derive them from the L1 key
           on the fly.  ``create_or_derive_api_creds`` is free — it
           signs once with our private key and Polymarket returns the
           deterministic key/secret/passphrase tied to that EOA.
        """
        if self._client is not None:
            return self._client

        try:
            # v2-2 (2026-04-27): switched from ``py_clob_client`` to
            # ``py_clob_client_v2`` ahead of the 2026-04-28 11:00 UTC
            # exchange cutover.  Same constructor kwargs (host, chain_id,
            # key, creds, signature_type, funder) — only the import path
            # changes here.  Method renames land in v2-3..v2-5.
            from py_clob_client_v2 import ClobClient as _ClobClient, ApiCreds
        except ImportError:
            logger.error(
                "py-clob-client-v2 not installed. "
                "Install with: pip install py-clob-client-v2"
            )
            raise

        funder = (self._config.funder_address or "").strip() or None
        signature_type = 2 if funder else 0
        if funder:
            logger.info(
                "CLOB init: proxy-wallet mode (funder=%s..., signature_type=2)",
                funder[:10],
            )
        else:
            logger.info(
                "CLOB init: direct EOA mode (no FUNDER_ADDRESS, signature_type=0)",
            )

        client = _ClobClient(
            "https://clob.polymarket.com",
            key=self._config.eth_private_key,
            chain_id=137,  # Polygon
            signature_type=signature_type,
            funder=funder,
        )

        api_key = (self._config.polymarket_api_key or "").strip()
        api_secret = (self._config.polymarket_secret or "").strip()
        api_pass = (self._config.polymarket_passphrase or "").strip()
        if api_key and api_secret and api_pass:
            logger.info("CLOB creds: using POLYMARKET_API_* from .env")
            creds = ApiCreds(
                api_key=api_key, api_secret=api_secret, api_passphrase=api_pass,
            )
        else:
            logger.info("CLOB creds: deriving from private key (free, signs once)")
            # v2-2 (2026-04-27): renamed in v2 SDK,
            # ``create_or_derive_api_creds`` → ``create_or_derive_api_key``.
            # Same semantics: signs once with the L1 key, server returns
            # the deterministic L2 ApiCreds tied to the EOA.
            creds = client.create_or_derive_api_key()
            # Defensive: py-clob-client has historically had silent-failure
            # paths that return None instead of raising when the upstream
            # /api-key endpoint returns a 5xx or a malformed body.  Without
            # this guard we'd cache a half-built ``client`` (no creds) on
            # ``self._client`` and only surface the failure at the FIRST
            # live BUY — by which point the operator may already be staring
            # at an inscrutable auth error mid-trade.  Fail fast here so
            # preflight + the startup banner catch it instead.
            if creds is None:
                raise RuntimeError(
                    "Polymarket create_or_derive_api_key returned None — "
                    "API likely returned a malformed response.  Check "
                    "https://clob.polymarket.com status; recovery: restart "
                    "the bot once Polymarket recovers.",
                )
        client.set_api_creds(creds)

        self._client = client
        return self._client

    async def get_orderbook(self, token_id: str) -> dict:
        """Get the order book for a token."""
        client = self._get_client()
        return await asyncio.to_thread(client.get_order_book, token_id)

    async def get_top_of_book(
        self, token_id: str,
    ) -> tuple[float | None, float | None]:
        """Returns ``(best_bid, best_ask)`` or ``(None, None)`` on empty book / error.

        Polymarket ``/book`` returns ``bids`` sorted desc by price and
        ``asks`` sorted asc.  An empty side maps to ``None`` so callers
        can treat it as a thin-liquidity skip rather than a hard error.

        Used by ``place_limit_order`` to convert a midpoint-derived limit
        into a cross-the-spread limit (see FAK-cross-pricing fix:
        midpoint + FAK is mathematically guaranteed to never fill, so
        we replace ``price`` with ``best_ask + 1tick`` for BUY /
        ``best_bid - 1tick`` for SELL just before submission).
        """
        if self._config.dry_run or self._config.paper:
            return None, None  # paper/dry-run never reach FAK; defensive
        client = self._get_client()
        try:
            book = await asyncio.to_thread(client.get_order_book, token_id)
        except Exception as exc:
            logger.warning(
                "get_top_of_book failed token=%s: %s", token_id[:12], exc,
            )
            return None, None
        # py_clob_client_v2 may return an OrderBookSummary object or a
        # plain dict depending on SDK version — normalise to dict-like.
        if not isinstance(book, dict):
            book = getattr(book, "__dict__", {}) or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bb: float | None = None
        ba: float | None = None
        if bids:
            try:
                top_bid = bids[0]
                bb = float(
                    top_bid["price"] if isinstance(top_bid, dict)
                    else getattr(top_bid, "price", 0.0)
                )
            except (KeyError, ValueError, TypeError):
                bb = None
        if asks:
            try:
                top_ask = asks[0]
                ba = float(
                    top_ask["price"] if isinstance(top_ask, dict)
                    else getattr(top_ask, "price", 0.0)
                )
            except (KeyError, ValueError, TypeError):
                ba = None
        return bb, ba

    async def get_midpoint(self, token_id: str) -> float | None:
        """Get the midpoint price for a token.

        v2-7 (2026-04-27): the CLOB ``/midpoint`` endpoint returns
        ``{"mid": "0.5"}`` — both v1 and v2 SDKs surface the raw JSON, so
        an unconditional ``float(result)`` raises ``TypeError`` against
        the dict.  Pre-v2 we never noticed because every recent run was
        paper / dry-run, where ``get_prices_batch()`` short-circuits
        before this method is called.  First live cycle on v2 caught it.
        """
        client = self._get_client()
        try:
            result = await asyncio.to_thread(client.get_midpoint, token_id)
            if isinstance(result, dict):
                return float(result.get("mid", result.get("midpoint", 0.0)))
            return float(result)
        except Exception:
            logger.exception("Failed to get midpoint for %s", token_id)
            return None

    async def get_conditional_balance(self, token_id: str) -> int:
        """Funder's on-chain ERC1155 balance for a CLOB ``token_id``.

        Returns raw 6-decimals USDC equivalent (1 share == 1_000_000).
        Mirrors ``polymarket_trade_bot.client.get_conditional_balance``:
        bypasses ConditionalTokens ``positionId`` encoding (which differs
        for negRisk markets — direct ``balanceOf`` on the standard CT
        with USDC as collateral always returns 0 for negRisk shares,
        which is what produced the false ``already_redeemed`` on the
        2026-04-28 Miami / Chicago redemptions).  The CLOB API knows how
        to look up balances per token_id regardless of negRisk flavor.

        Returns 0 on any failure — caller treats 0 as "nothing to redeem"
        idempotently (worst case: defer one cycle and retry).
        """
        client = self._get_client()
        try:
            from py_clob_client_v2.clob_types import (
                AssetType, BalanceAllowanceParams,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            result = await asyncio.to_thread(
                client.get_balance_allowance, params,
            )
            if not isinstance(result, dict):
                return 0
            return int(result.get("balance", 0))
        except Exception:
            logger.exception(
                "get_conditional_balance failed for token=%s",
                token_id[:16] + "...",
            )
            return 0

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        idempotency_key: str | None = None,
        strategy_config: object | None = None,
    ) -> OrderResult:
        """Place a limit order.

        ``idempotency_key`` is not accepted by py-clob-client itself but is
        threaded through so logs/paper-returns carry the breadcrumb the
        reconciler (FIX-05) keys off of.

        ``strategy_config`` is an optional ``StrategyConfig`` (the type is
        elided to avoid a circular import — duck-typed via ``getattr``).
        Used to read ``max_taker_slippage`` for the cross-spread gate.  The
        executor passes the active variant's config; falling back to
        ``self._config.strategy`` lets paper / test paths that don't thread
        the variant through still see the YAML-tuned value.  Final fallback
        is the hardcoded 5% so the gate never disappears.
        """
        key_suffix = f" key={idempotency_key[:8]}" if idempotency_key else ""

        # FAK cold-start guard (2026-04-30, review #4): a cold-start Gamma 0
        # price can leak past PriceStopGate via the 15-min position-check
        # path (see CLAUDE.md "Position-check cycle bypasses D1's discovery
        # filter").  Bailing here means the FAK matcher never sees a 0 limit
        # — both because Polymarket would reject it as below tick, and
        # because it'd compute a nonsense slippage ratio (denominator → 0).
        # ``<=`` covers 0, negatives, and the tick boundary itself; the
        # latter is intentional because a real entry at exactly the tick
        # floor would still be sub-cent EV after the slippage gate.
        # Note: SELL force-exit at floor (0.01) is also blocked here — those
        # positions wait for settlement payout instead of attempting to sell
        # below tick (which the orderbook can't accept anyway).
        if price <= TICK:
            logger.warning(
                "Order skipped PRICE_TOO_LOW_FAK_GUARD token=%s side=%s "
                "mid=%.4f tick=%.4f%s — likely cold-start Gamma=0 leak past "
                "PriceStopGate; defensive guard, not the upstream fix",
                token_id[:12], side, price, TICK, key_suffix,
            )
            return OrderResult(
                order_id="", success=False,
                message="PRICE_TOO_LOW_FAK_GUARD",
            )

        if self._config.dry_run:
            logger.info(
                "[DRY RUN] Would place %s order: token=%s price=%.4f size=%.2f%s",
                side, token_id, price, size, key_suffix,
            )
            return OrderResult(order_id="dry_run", success=False, message="dry run — no positions recorded")

        if self._config.paper:
            logger.info(
                "[PAPER] Simulated %s fill: token=%s price=%.4f size=%.2f%s",
                side, token_id, price, size, key_suffix,
            )
            # Paper order_ids must be unique per order so the orders-table UNIQUE
            # index (and the reconciler) can tell them apart.  Using the
            # idempotency key (first 12 hex chars) gives a stable, collision-free
            # handle without leaking the full uuid into logs.
            suffix = idempotency_key[:12] if idempotency_key else token_id[:8]
            return OrderResult(order_id=f"paper_{suffix}", success=True, message="paper trade")

        client = self._get_client()
        # v2-3 (2026-04-27): v2 SDK requires a typed ``OrderArgs`` (not
        # the raw dict the v1 SDK accepted) plus an explicit
        # ``OrderType``.  The third method arg ``options`` stays None
        # so the server picks the tick size dynamically — Polymarket's
        # weather slots have used 0.01 ticks consistently and we don't
        # want a hard-coded value to drift if microstructure changes.
        #
        # 2026-04-28: switched from ``OrderType.GTC`` to ``OrderType.FAK``.
        # Rationale: our EV / fee model assumes taker semantics
        # (``gates.py::entry_fee_per_dollar`` is computed at the taker
        # rate, never the maker rebate), and the BUY price we pass is
        # ``Gamma outcomePrices[1]`` — the *last-trade* price, not the
        # best ask.  When the live book's best ask is above last-trade,
        # a GTC limit at last-trade rests on the book as a maker.  Two
        # downstream bugs emerged:
        #   (1) The wrapper's matched-detection (``status='matched'``
        #       vs anything else) treated resting orders as failures,
        #       which produced a "failed" orders row + a phantom fill
        #       risk if the resting order later matched.
        #   (2) The previous A1 fix in ``executor.py`` then cancelled
        #       our own legitimate resting maker order.
        # FAK ("Fill And Kill") forces the server to immediately fill
        # whatever crosses and kill the remainder.  No resting state is
        # possible, so the status ladder collapses to:
        #   - matched / partial-fill (with tx hashes) → success
        #   - cancelled / killed (no fill)            → failure
        # That aligns with the taker-only EV model and removes the
        # "phantom resting order" class of bug at its source.
        from py_clob_client_v2 import OrderArgs, OrderType, Side

        order_side = Side.BUY if side.upper() == "BUY" else Side.SELL

        # FAK-cross-pricing fix (2026-04-30): the ``price`` we receive from
        # the strategy layer is a CLOB-midpoint or last-trade price (see
        # ``get_prices_batch`` → ``get_midpoint`` fallback chain).  Submitting
        # a FAK order at midpoint is mathematically guaranteed to never fill:
        # for any positive spread, midpoint < best_ask AND midpoint > best_bid,
        # so the FAK matcher finds nothing to cross and the server either
        # 400's "no orders found to match" or 200's status=delayed and kills
        # async.  Both paths surfaced in production 2026-04-29 (1/19 BUY fill,
        # 0/16 SELL fill in the FAK era).
        #
        # The fix is structural: pre-flight the order book, then replace
        # ``price`` with ``best_ask + 1 tick`` (BUY) or ``best_bid - 1 tick``
        # (SELL).  The 1-tick safety margin absorbs sub-RTT book moves; FAK
        # kills any unfilled remainder server-side so the extra tick is never
        # actually paid (server fills at the resting maker price, not our
        # cap).
        #
        # Two skip paths short-circuit before submission:
        #   - THIN_LIQUIDITY_NO_ASK / NO_BID: book is empty on the side we'd
        #     cross.  Bailing here avoids a guaranteed FAK reject and the
        #     accompanying retry storm.
        #   - SLIPPAGE_TOO_HIGH: cross_price diverges from midpoint by more
        #     than ``max_taker_slippage`` (default 5%).  Catches Atlanta-style
        #     near-settled books where last-trade is 0.20 but the only
        #     remaining bid is 0.001 — taking that fill would crystallise a
        #     loss the strategy didn't price in.
        # ``TICK`` lives at module scope (used by the cold-start guard
        # earlier in this method too).  ``max_taker_slippage`` resolution
        # order: explicit ``strategy_config`` arg → ``self._config.strategy``
        # (live config from config.yaml) → hardcoded 5% safety net.
        MAX_TAKER_SLIPPAGE = 0.05
        if strategy_config is not None:
            MAX_TAKER_SLIPPAGE = getattr(
                strategy_config, "max_taker_slippage", MAX_TAKER_SLIPPAGE,
            )
        else:
            base_strategy = getattr(self._config, "strategy", None)
            if base_strategy is not None:
                MAX_TAKER_SLIPPAGE = getattr(
                    base_strategy, "max_taker_slippage", MAX_TAKER_SLIPPAGE,
                )

        bb, ba = await self.get_top_of_book(token_id)

        if side.upper() == "BUY":
            if ba is None:
                logger.info(
                    "BUY skipped THIN_LIQUIDITY_NO_ASK token=%s mid=%.4f "
                    "size=%.4f%s",
                    token_id[:12], price, size, key_suffix,
                )
                return OrderResult(
                    order_id="", success=False,
                    message="THIN_LIQUIDITY_NO_ASK",
                )
            cross_price = round(ba + TICK, 2)
            if cross_price > 1.0:
                cross_price = 1.0  # Polymarket prices cap at 1.0 USDC
            slip = (cross_price - price) / max(price, TICK)
            if slip > MAX_TAKER_SLIPPAGE:
                logger.info(
                    "BUY skipped SLIPPAGE_TOO_HIGH token=%s mid=%.4f "
                    "ask=%.4f cross=%.4f slip=%.2f%% gate=%.2f%%%s",
                    token_id[:12], price, ba, cross_price,
                    slip * 100, MAX_TAKER_SLIPPAGE * 100, key_suffix,
                )
                return OrderResult(
                    order_id="", success=False,
                    message=(
                        f"SLIPPAGE_TOO_HIGH ask={ba:.4f} mid={price:.4f}"
                    ),
                )
        else:  # SELL
            if bb is None:
                logger.info(
                    "SELL skipped THIN_LIQUIDITY_NO_BID token=%s mid=%.4f "
                    "size=%.4f%s",
                    token_id[:12], price, size, key_suffix,
                )
                return OrderResult(
                    order_id="", success=False,
                    message="THIN_LIQUIDITY_NO_BID",
                )
            cross_price = round(bb - TICK, 2)
            if cross_price < TICK:
                cross_price = TICK  # don't go below min tick
            slip = (price - cross_price) / max(price, TICK)
            if slip > MAX_TAKER_SLIPPAGE:
                logger.info(
                    "SELL skipped SLIPPAGE_TOO_HIGH token=%s mid=%.4f "
                    "bid=%.4f cross=%.4f slip=%.2f%% gate=%.2f%%%s",
                    token_id[:12], price, bb, cross_price,
                    slip * 100, MAX_TAKER_SLIPPAGE * 100, key_suffix,
                )
                return OrderResult(
                    order_id="", success=False,
                    message=(
                        f"SLIPPAGE_TOO_HIGH bid={bb:.4f} mid={price:.4f}"
                    ),
                )

        logger.info(
            "Cross-the-spread token=%s side=%s mid=%.4f best_%s=%.4f "
            "→ cross=%.4f size=%.4f%s",
            token_id[:12], side, price,
            "ask" if side.upper() == "BUY" else "bid",
            ba if side.upper() == "BUY" else bb,
            cross_price, size, key_suffix,
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=cross_price,
            size=size,
            side=order_side,
        )

        # FIX-04: bounded timeout, retry with exponential backoff on transient
        # failures, distinct longer backoff on 429.  Without the timeout, a hung
        # call to create_and_post_order freezes the asyncio.to_thread executor
        # indefinitely — in prod we've seen 60+ minute hangs.
        last_exc: Exception | None = None
        for attempt in range(ORDER_MAX_ATTEMPTS):
            try:
                async with asyncio.timeout(ORDER_TIMEOUT_S):
                    order = await asyncio.to_thread(
                        client.create_and_post_order,
                        order_args,
                        None,  # PartialCreateOrderOptions: server picks tick
                        OrderType.FAK,
                    )
                order_id = (
                    order.get("orderID", "") if isinstance(order, dict) else str(order)
                )
                # Ghost-position guard (2026-04-28): v2 ``create_and_post_order``
                # returns the same dict shape for both immediately-matched fills
                # and orders that didn't fully fill.  Pre-fix, the wrapper
                # treated any non-empty ``orderID`` as a successful fill, so
                # executor would write a positions row for an order that only
                # sat on the book — producing a ghost row whose live "position"
                # was actually never owned (e.g. Miami 86-87 NO @0.565 on
                # 2026-04-28).  Real fills carry ``status=='matched'`` AND/OR
                # at least one transactionsHashes entry.  Under FAK (the order
                # type we use, see above) any unfilled remainder is killed by
                # the server, so the residual statuses are typically
                # ``cancelled`` / ``killed`` rather than the GTC-era ``live``
                # / ``unmatched`` — but the detection logic is the same shape
                # ("not matched, no tx hashes → failure").
                if isinstance(order, dict):
                    status = str(order.get("status", "")).lower()
                    tx_hashes = (
                        order.get("transactionsHashes")
                        or order.get("transaction_hashes")
                        or []
                    )
                else:
                    status = ""
                    tx_hashes = []
                matched = status == "matched" or bool(tx_hashes)
                logger.info(
                    "Order placed: %s %s @ %.4f x %.2f -> %s status=%s matched=%s%s (attempt=%d)",
                    side, token_id, cross_price, size, order_id,
                    status or "?", matched, key_suffix, attempt + 1,
                )
                # FIX-M4: empty order_id is treated as failure (see above).
                if not order_id:
                    return OrderResult(
                        order_id="", success=False,
                        message="CLOB returned empty order_id",
                    )
                if not matched:
                    # FAK: server has already killed any unfilled remainder,
                    # so there is no resting order to reconcile or cancel.
                    # Surface as success=False so the executor records the
                    # orders row as 'failed' (no positions row created); on
                    # the next cycle the strategy layer will re-evaluate and
                    # re-emit if the entry condition still holds.
                    return OrderResult(
                        order_id=order_id, success=False,
                        message=f"order not filled (status={status or 'unknown'})",
                    )
                return OrderResult(order_id=order_id, success=True)
            except TimeoutError as e:
                last_exc = e
                logger.error(
                    "place_limit_order TIMED OUT after %.0fs (attempt=%d/%d) — "
                    "NOT retrying.  The underlying HTTP POST may have reached CLOB "
                    "and created an order despite the Python-side timeout.  "
                    "Startup reconciler will resolve on next restart.",
                    ORDER_TIMEOUT_S, attempt + 1, ORDER_MAX_ATTEMPTS,
                )
                if not TIMEOUT_RETRIES:
                    return OrderResult(
                        order_id="", success=False,
                        message=(
                            "timeout after %.0fs — NOT retried to avoid double-order "
                            "(CLOB may still have accepted); reconciler will resolve"
                        ) % ORDER_TIMEOUT_S,
                    )
            except Exception as e:
                last_exc = e
                if _is_rate_limit_error(e):
                    sleep_s = min(2 ** attempt + random.random(), RATE_LIMIT_CAP_S)
                    logger.warning(
                        "CLOB 429 on place_limit_order (attempt=%d/%d), "
                        "backing off %.2fs",
                        attempt + 1, ORDER_MAX_ATTEMPTS, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    continue
                if _is_order_version_mismatch_error(e):
                    refreshed = _force_refresh_clob_version(client)
                    logger.warning(
                        "order_version_mismatch on place_limit_order "
                        "(attempt=%d/%d) — %s, retrying",
                        attempt + 1, ORDER_MAX_ATTEMPTS,
                        "force-refreshed CLOB version cache"
                        if refreshed else "cache-refresh FAILED",
                    )
                    # Fall through to the standard inter-attempt backoff so
                    # a transiently-flapping cutover doesn't get hammered.
                logger.warning(
                    "place_limit_order failed (attempt=%d/%d): %s",
                    attempt + 1, ORDER_MAX_ATTEMPTS, e,
                )
            # Non-rate-limit retriable path: short exponential backoff between
            # retries so we don't hammer the CLOB. Skip the sleep on the final
            # attempt since we're about to return anyway.
            if attempt < ORDER_MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (2 ** attempt) + random.random() * 0.25)

        logger.exception("Order placement exhausted retries", exc_info=last_exc)
        return OrderResult(
            order_id="", success=False, message=str(last_exc) if last_exc else "retries exhausted",
        )

    async def get_fill_summary(
        self,
        *,
        token_id: str,
        order_id: str,
        created_at_epoch: int | None = None,
    ) -> "FillSummary | None":
        """Fetch trades for ``order_id`` and aggregate effective price + fees.

        Used by the executor right after a matched BUY to record the actual
        per-share cost (limit price was 0.69 but the fill may have crossed
        at 0.685 due to a thin-side maker) plus the taker fee paid.

        Returns ``None`` in paper / dry-run mode (no real trades), and on
        any SDK error — callers fall back to limit price + NULL fee.

        Field-name resilience: Polymarket's ``/data/trades`` response is
        forwarded as raw JSON.  ``taker_order_id`` is the documented field
        keying a trade to the order that initiated it; the per-trade
        ``fee_rate_bps`` is the COMBINED maker+taker bps (5%+5%=1000), so
        our taker share is bps/2/10000 — multiplied by ``size`` and
        ``price * (1 - price)`` per Polymarket's fee formula (matches the
        prod-verified math: 3.12 × 0.05 × 0.69 × 0.31 = $0.0334 for the
        2026-04-28 Miami trade).  If a future SDK adds an explicit
        ``fee_paid`` per-trade field, that's preferred — checked first.
        """
        if self._config.dry_run or self._config.paper:
            return None
        if not order_id:
            return None

        try:
            from py_clob_client_v2.clob_types import TradeParams
        except ImportError:
            logger.warning("get_fill_summary: py-clob-client-v2 not installed")
            return None

        client = self._get_client()
        try:
            params = TradeParams(asset_id=token_id, after=created_at_epoch)
            resp = await asyncio.to_thread(client.get_trades, params)
        except Exception:
            logger.exception("get_fill_summary: get_trades failed")
            return None

        trades = _extract_list(resp)
        matching: list[dict] = []
        for t in trades:
            taker_id = (
                t.get("taker_order_id")
                or t.get("takerOrderID")
                or t.get("taker_orderID")
                or ""
            )
            if str(taker_id) == str(order_id):
                matching.append(t)
        if not matching:
            return None

        total_shares = 0.0
        total_usdc = 0.0
        total_fee = 0.0
        total_net_shares = 0.0
        for t in matching:
            try:
                size = float(t.get("size", 0) or 0)
                price = float(t.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0 or price <= 0:
                continue
            total_shares += size
            total_usdc += size * price
            # Bug C (2026-04-29): Polymarket BUY taker fee is deducted in
            # shares from the token side, not USDC.  Compute the per-trade
            # fee twice — once in USDC (legacy field, still useful for P&L
            # reporting) and once in shares (what's actually missing on
            # chain).  Prefer explicit ``fee_paid`` when present (source-
            # of-truth, no derivation), else derive from ``fee_rate_bps``.
            #   trade_fee_usdc = size × taker_rate × price × (1 − price)
            #   trade_fee_shares = trade_fee_usdc / price = size × taker_rate × (1 − price)
            # Verified 2026-04-29 against id=6 Denver: bps=1000, size=3.08,
            # price=0.78 → fee_shares = 3.08 × 0.05 × 0.22 = 0.03388, on-chain
            # = 3.08 − 0.03388 = 3.04612 (raw 3046120) ✓.
            trade_fee_usdc = 0.0
            explicit_fee = t.get("fee_paid") or t.get("fee_paid_usd")
            if explicit_fee is not None:
                try:
                    trade_fee_usdc = float(explicit_fee)
                except (TypeError, ValueError):
                    explicit_fee = None
            if explicit_fee is None:
                try:
                    bps_combined = float(t.get("fee_rate_bps", 0) or 0)
                except (TypeError, ValueError):
                    bps_combined = 0.0
                taker_rate = (bps_combined / 2.0) / 10000.0
                trade_fee_usdc = size * taker_rate * price * (1.0 - price)
            total_fee += trade_fee_usdc
            # On-chain net shares for this trade: gross size minus the fee
            # converted back to shares at the trade's own price.  Falls
            # naturally to ``size`` when fee is zero (zero-fee tier or
            # missing bps).
            trade_fee_shares = trade_fee_usdc / price if price > 0 else 0.0
            total_net_shares += size - trade_fee_shares

        if total_shares <= 0:
            return None
        return FillSummary(
            shares=total_shares,
            match_price=total_usdc / total_shares,
            fee_paid_usd=total_fee,
            net_shares=total_net_shares,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._config.dry_run:
            logger.info("[DRY RUN] Would cancel order %s", order_id)
            return True

        client = self._get_client()
        try:
            # v2-4 (2026-04-27): v1 SDK exposed ``client.cancel(order_id)``
            # taking a bare string; v2 SDK splits that into:
            #   - ``cancel_order(payload: OrderPayload)`` — single
            #   - ``cancel_orders(order_hashes: list)`` — batch
            #   - ``cancel_all()`` — every open order for this account
            # We always cancel exactly one known order_id, so the
            # single-shape is the right replacement.  Note the field
            # name is ``orderID`` (camelCase) not ``order_id``.
            from py_clob_client_v2.clob_types import OrderPayload
            await asyncio.to_thread(
                client.cancel_order, OrderPayload(orderID=order_id),
            )
            logger.info("Order cancelled: %s", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def get_last_trade_price(self, token_id: str) -> float | None:
        """Get the last trade price for a token via CLOB API.

        v2-7 (2026-04-27): same dict-vs-float gotcha as ``get_midpoint``.
        ``/last-trade-price`` returns ``{"price": "0.5", "side": "BUY"}``
        — extract the ``price`` field before float-coercing.
        """
        if self._config.dry_run:
            return None  # no real prices in dry-run
        client = self._get_client()
        try:
            result = await asyncio.to_thread(client.get_last_trade_price, token_id)
            if isinstance(result, dict):
                return float(result.get("price", result.get("lastTradePrice", 0.0)))
            return float(result)
        except Exception:
            logger.debug("Failed to get last trade price for %s", token_id)
            return None

    async def get_prices_batch(self, token_ids: list[str]) -> dict[str, float]:
        """Get current prices for multiple tokens. Returns available prices.

        In dry-run/paper mode, returns empty dict (no CLOB auth available).
        Callers should fallback to Gamma API prices.
        """
        if self._config.dry_run or self._config.paper:
            return {}

        prices: dict[str, float] = {}
        for token_id in token_ids:
            price = await self.get_midpoint(token_id) or await self.get_last_trade_price(token_id)
            if price is not None:
                prices[token_id] = price
        return prices

    async def get_positions(self) -> list[Position]:
        """Get all open positions (simplified)."""
        # Note: py-clob-client doesn't have a direct get_positions method.
        # Positions are typically tracked locally via our portfolio module.
        # This is a placeholder for any future API integration.
        return []

    async def probe_order_status(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        created_at_epoch: int | None = None,
    ) -> "ProbeResult":
        """Review Blocker #2: look up CLOB for an order matching our intent.

        Used by the startup reconciler to resolve a pending orders row
        when we crashed between `create_and_post_order` and the local
        position insert.  py-clob-client does NOT expose our client-side
        idempotency_key to Polymarket, so we match by the observable
        quadruple (token_id, side, price, size_shares).  This is the
        best we can do without a server-side dedup key.

        Returns ProbeResult(state='filled'|'open'|'unknown'|'unreachable', ...).

        Strategy:
        1. Call get_trades(asset_id=token_id, after=created_at_epoch-300)
           and scan for a matching trade.  If found → 'filled'.
        2. Else call get_open_orders(asset_id=token_id) and scan for a
           matching open order.  If found → 'open' (resting limit on CLOB).
        3. Else → 'unknown' (safe to mark failed; a subsequent manual
           review of CLOB trade history confirms).

        Paper/dry-run: returns 'unreachable' immediately (there is no
        real CLOB state to probe).
        """
        if self._config.dry_run or self._config.paper:
            return ProbeResult(
                state="unreachable",
                message="paper/dry-run: no live CLOB to probe",
            )

        try:
            # v2-5 (2026-04-27): import path moved to py_clob_client_v2
            # ahead of the 2026-04-28 exchange cutover.  Param dataclass
            # fields (asset_id / after) unchanged from v1.
            from py_clob_client_v2.clob_types import (
                OpenOrderParams, TradeParams,
            )
        except ImportError:
            return ProbeResult(
                state="unreachable",
                message="py-clob-client-v2 not installed",
            )

        client = self._get_client()
        # Review H-3 (2026-04-24): widened from 0.005 to 0.01 so a 5-tick
        # price-improvement fill (e.g. a BUY@0.50 limit that filled at 0.494
        # because a sell-side order crossed our bid) still matches in the
        # reconciler.  5 ticks was conservative for exact-match reasoning
        # but too strict for real market microstructure; 10 ticks is still
        # tight enough that an unrelated order at ±0.05 won't be mistaken
        # for our intent.
        tolerance_price = 0.01
        tolerance_size = 0.5

        def _match_trade(trade: dict) -> bool:
            try:
                tside = str(trade.get("side", "")).upper()
                tprice = float(trade.get("price", 0))
                tsize = float(trade.get("size", 0))
            except (TypeError, ValueError):
                return False
            if tside != side.upper():
                return False
            if abs(tprice - price) > tolerance_price:
                return False
            if abs(tsize - size_shares) > tolerance_size:
                return False
            return True

        def _match_order(order: dict) -> bool:
            """Match a live CLOB order against the pending intent.

            Review H-7 (2026-04-24): a partially-filled order reports
            original_size for its intent and size_matched for the filled
            portion.  The still-live remainder is (original - matched).
            Pre-H-7 we matched on original_size only, which meant a 10-share
            intent whose first 3 shares already filled looked like a
            "7-share open order", not matching our "10-share pending" row
            by the 0.5-share tolerance.  Now we compute remaining and
            compare to the intent — still-open orders are correctly
            identified even after a partial fill.
            """
            try:
                oside = str(order.get("side", "")).upper()
                oprice = float(order.get("price", 0))
                original = float(order.get("original_size", order.get("size", 0)))
                matched = float(order.get("size_matched", 0))
                remaining = original - matched if original else 0.0
            except (TypeError, ValueError):
                return False
            if oside != side.upper():
                return False
            if abs(oprice - price) > tolerance_price:
                return False
            # Our intent was size_shares; the remainder on the book must
            # match that (partial fills leave (size - filled) resting).
            if abs(remaining - size_shares) > tolerance_size and (
                # Fall-through: an order with no size_matched field (some
                # API shapes omit it) should still match if original ≈ intent.
                abs(original - size_shares) > tolerance_size
            ):
                return False
            return True

        try:
            # Probe trades (confirmed fills) first.
            trade_params = TradeParams(
                asset_id=token_id, after=created_at_epoch,
            )
            trades_resp = await asyncio.to_thread(client.get_trades, trade_params)
            trades_list = _extract_list(trades_resp)
            for t in trades_list:
                if _match_trade(t):
                    return ProbeResult(
                        state="filled",
                        order_id=str(t.get("id") or t.get("order_id") or ""),
                        price=float(t.get("price", 0)),
                        size=float(t.get("size", 0)),
                        message="matched via get_trades",
                    )

            # Probe open orders (still resting).
            # v2-5 (2026-04-27): v1 SDK ``client.get_orders(params)`` →
            # v2 SDK ``client.get_open_orders(params)``.  Same param
            # shape (``OpenOrderParams(asset_id=...)``); only the method
            # name changed.
            oparams = OpenOrderParams(asset_id=token_id)
            orders_resp = await asyncio.to_thread(client.get_open_orders, oparams)
            orders_list = _extract_list(orders_resp)
            for o in orders_list:
                if _match_order(o):
                    return ProbeResult(
                        state="open",
                        order_id=str(o.get("id") or o.get("order_id") or ""),
                        price=float(o.get("price", 0)),
                        size=float(o.get("original_size", o.get("size", 0))),
                        message="matched via get_open_orders",
                    )
        except Exception as exc:
            logger.exception("probe_order_status raised")
            return ProbeResult(state="unreachable", message=str(exc))

        return ProbeResult(
            state="unknown",
            message="no matching trade or open order on CLOB (safe to mark failed)",
        )


@dataclass
class ProbeResult:
    """Reconciler-facing view of CLOB state for one pending intent."""
    state: str  # 'filled' | 'open' | 'unknown' | 'unreachable'
    order_id: str = ""
    price: float | None = None
    size: float | None = None
    message: str = ""


def _extract_list(resp) -> list[dict]:
    """py-clob-client sometimes returns {'data': [...], 'next_cursor': '...'}
    and sometimes a bare list; normalise both."""
    if isinstance(resp, dict) and "data" in resp:
        v = resp["data"]
        return v if isinstance(v, list) else []
    if isinstance(resp, list):
        return resp
    return []
