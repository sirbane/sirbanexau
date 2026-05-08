"""
risk_guard.py  ─  Scuro Pre-Trade Risk Validation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Called immediately before every order_send() in xau.py.
Catches bad SL/TP math, validates lot size, checks session
loss limits, and confirms position count in real time.

Import in xau.py:
    from risk_guard import validate_trade, RiskGuardError

Usage:
    try:
        validate_trade(signal, price, sl, tp, lot, session_pnl,
                       account_balance, open_count, max_positions)
    except RiskGuardError as e:
        logger.error(f"❌ Risk guard blocked trade: {e}")
        continue  # or skip to next loop iteration
"""

import logging
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

# ─── CONSTANTS ───────────────────────────────────────────────
MAX_DAILY_LOSS_PCT      = 0.15   # stop all trading if session loss > 15% of balance
MAX_SL_PIPS             = 500    # SL wider than 500 pips = almost certainly a bug
MIN_SL_PIPS             = 5      # SL tighter than 5 pips = almost certainly a bug
MAX_TP_SL_RATIO         = 10.0   # TP/SL ratio above 10 is unrealistic
MIN_TP_SL_RATIO         = 0.5    # TP/SL ratio below 0.5 = bad risk/reward
MIN_LOT                 = 0.01
MAX_LOT                 = 0.05   # hard ceiling regardless of advisor config
POINT_VALUE             = 0.1    # XAUUSDm: 1 pip = 0.1 price units (verify with your broker)


class RiskGuardError(Exception):
    """Raised when a trade fails a risk validation check."""
    pass


# ─── MAIN VALIDATOR ──────────────────────────────────────────

def validate_trade(
    signal: str,
    price: float,
    sl: float,
    tp: float,
    lot: float,
    session_pnl: float,
    account_balance: float,
    open_count: int,
    max_positions: int,
    symbol: str = "XAUUSDm",
    magic: int = 777777,
) -> bool:
    """
    Runs all pre-trade safety checks. Raises RiskGuardError on failure.
    Returns True if all checks pass.

    Checks performed:
      1. SL is on the correct side of entry (BUY: SL < price, SELL: SL > price)
      2. TP is on the correct side of entry
      3. SL distance is within sane pip range
      4. TP/SL ratio is reasonable
      5. Lot size is within bounds
      6. Daily loss limit not breached
      7. Open position count (live re-query from MT5)
    """

    # ── 1. SL direction check ─────────────────────────────────
    if signal == "BUY" and sl >= price:
        raise RiskGuardError(
            f"SL INVERSION on BUY: SL={sl:.2f} >= price={price:.2f}. "
            f"SL must be BELOW entry on a buy."
        )
    if signal == "SELL" and sl <= price:
        raise RiskGuardError(
            f"SL INVERSION on SELL: SL={sl:.2f} <= price={price:.2f}. "
            f"SL must be ABOVE entry on a sell."
        )

    # ── 2. TP direction check ─────────────────────────────────
    if signal == "BUY" and tp <= price:
        raise RiskGuardError(
            f"TP INVERSION on BUY: TP={tp:.2f} <= price={price:.2f}. "
            f"TP must be ABOVE entry on a buy."
        )
    if signal == "SELL" and tp >= price:
        raise RiskGuardError(
            f"TP INVERSION on SELL: TP={tp:.2f} >= price={price:.2f}. "
            f"TP must be BELOW entry on a sell."
        )

    # ── 3. SL distance sanity ─────────────────────────────────
    sl_dist_price = abs(price - sl)
    sl_pips       = sl_dist_price / POINT_VALUE

    if sl_pips > MAX_SL_PIPS:
        raise RiskGuardError(
            f"SL TOO WIDE: {sl_pips:.0f} pips (max {MAX_SL_PIPS}). "
            f"price={price:.2f} sl={sl:.2f}"
        )
    if sl_pips < MIN_SL_PIPS:
        raise RiskGuardError(
            f"SL TOO TIGHT: {sl_pips:.1f} pips (min {MIN_SL_PIPS}). "
            f"price={price:.2f} sl={sl:.2f}"
        )

    # ── 4. TP/SL ratio check ──────────────────────────────────
    tp_dist  = abs(tp - price)
    sl_dist  = abs(sl - price)
    rr_ratio = tp_dist / sl_dist if sl_dist > 0 else 0

    if rr_ratio > MAX_TP_SL_RATIO:
        raise RiskGuardError(
            f"TP/SL RATIO UNREALISTIC: {rr_ratio:.1f}x (max {MAX_TP_SL_RATIO}x)"
        )
    if rr_ratio < MIN_TP_SL_RATIO:
        raise RiskGuardError(
            f"TP/SL RATIO TOO LOW: {rr_ratio:.2f}x (min {MIN_TP_SL_RATIO}x) — "
            f"bad risk/reward, skipping trade"
        )

    # ── 5. Lot size check ─────────────────────────────────────
    if lot < MIN_LOT or lot > MAX_LOT:
        raise RiskGuardError(
            f"LOT OUT OF BOUNDS: {lot} (allowed {MIN_LOT}–{MAX_LOT})"
        )

    # ── 6. Daily loss limit ───────────────────────────────────
    if account_balance > 0:
        loss_pct = abs(min(session_pnl, 0)) / account_balance
        if loss_pct >= MAX_DAILY_LOSS_PCT:
            raise RiskGuardError(
                f"DAILY LOSS LIMIT: session P&L={session_pnl:+.0f} KES "
                f"({loss_pct:.1%} of balance {account_balance:.0f}). "
                f"Max daily loss is {MAX_DAILY_LOSS_PCT:.0%}. Trading halted."
            )

    # ── 7. Live position count re-check ──────────────────────
    # This is the final gate — re-queries MT5 in real time immediately
    # before order_send to catch any race condition.
    live_positions = mt5.positions_get(symbol=symbol, magic=magic)
    live_count     = len(live_positions) if live_positions else 0

    if live_count >= max_positions:
        raise RiskGuardError(
            f"POSITION LIMIT (live check): {live_count}/{max_positions} open. "
            f"Skipping trade."
        )

    # ── All clear ─────────────────────────────────────────────
    logger.info(
        f"✅ Risk guard passed: {signal} @ {price:.2f} | "
        f"SL={sl:.2f} ({sl_pips:.0f}p) TP={tp:.2f} | "
        f"RR={rr_ratio:.2f}x | lot={lot} | "
        f"positions={live_count}/{max_positions}"
    )
    return True


# ─── SESSION P&L HELPER ──────────────────────────────────────

def get_session_pnl(symbol: str = "XAUUSDm", magic: int = 777777) -> float:
    """
    Computes today's realised + floating P&L directly from MT5.
    Used to pass session_pnl into validate_trade() and ai_context.py.

    Returns total P&L in account currency (KES for your setup).
    """
    from datetime import datetime, timedelta

    try:
        # Closed trades today
        date_from = datetime.now().replace(hour=0, minute=0, second=0)
        date_to   = datetime.now() + timedelta(hours=1)
        deals     = mt5.history_deals_get(date_from, date_to)

        realised = 0.0
        if deals:
            for d in deals:
                if d.symbol == symbol and d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
                    realised += d.profit

        # Floating P&L on open positions
        positions = mt5.positions_get(symbol=symbol, magic=magic)
        floating  = sum(p.profit for p in positions) if positions else 0.0

        total = round(realised + floating, 2)
        return total

    except Exception as e:
        logger.warning(f"⚠️  get_session_pnl error: {e} — returning 0")
        return 0.0


def get_last_n_outcomes(
    n: int = 5,
    symbol: str = "XAUUSDm",
    magic: int = 777777,
) -> list[str]:
    """
    Returns the last N trade outcomes as a list of "WIN" / "LOSS" / "BE"
    pulled directly from MT5 history.
    Used to pass last_5_outcomes into ai_context.py.
    """
    from datetime import datetime, timedelta

    try:
        date_from = datetime.now() - timedelta(days=7)
        date_to   = datetime.now() + timedelta(days=1)
        deals     = mt5.history_deals_get(date_from, date_to)

        if not deals:
            return []

        outcomes = []
        for d in sorted(deals, key=lambda x: x.time):
            if d.symbol == symbol and d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
                if d.profit > 0:
                    outcomes.append("WIN")
                elif d.profit < 0:
                    outcomes.append("LOSS")
                else:
                    outcomes.append("BE")

        return outcomes[-n:]

    except Exception as e:
        logger.warning(f"⚠️  get_last_n_outcomes error: {e} — returning []")
        return []
