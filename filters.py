"""
filters.py  ─  Scuro Signal Quality Gates
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provides pre-trade filters that xau.py calls BEFORE sending any signal
to the AI or placing a trade.

Filters:
  1. ADX ranging filter     — blocks trades when market is sideways
  2. RSI multi-length check — uses RSI(5), RSI(10), RSI(14) consensus
  3. Signal quality score   — returns a 0–100 score for logging/tuning
  4. Candle body filter     — blocks trades on doji/indecision candles

Import in xau.py:
    from filters import is_market_trending, signal_quality_score, candle_is_decisive
"""

import pandas_ta as ta
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# ─── TUNABLE CONSTANTS ───────────────────────────────────────
ADX_PERIOD        = 14
ADX_MIN_TREND     = 20      # below this = ranging, don't trade TREND signals
ADX_STRONG_TREND  = 25      # above this = strong trend, higher confidence
DOJI_BODY_PCT     = 0.25    # candle body < 25% of total range = doji, skip
RSI_AGREE_MARGIN  = 5       # multi-RSI values must agree within this margin


# ─── 1. ADX RANGING FILTER ───────────────────────────────────

def is_market_trending(df: pd.DataFrame, signal_type: str = "TREND") -> tuple[bool, float]:
    """
    Returns (is_trending: bool, adx_value: float).

    BREAKOUT signals use a lower ADX threshold — a breakout can start
    from a compressed/ranging market so we don't want to block it.
    TREND and DIP signals require ADX > ADX_MIN_TREND.

    Args:
        df:          M5 DataFrame with high, low, close columns
        signal_type: "TREND", "DIP", or "BREAKOUT"

    Usage in xau.py:
        trending, adx_val = is_market_trending(df, signal_type)
        if not trending:
            logger.info(f"⛔ ADX filter blocked {signal_type} — ADX={adx_val:.1f}")
            signal = "NONE"
    """
    try:
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=ADX_PERIOD)
        if adx_df is None or adx_df.empty:
            # Can't compute — don't block, just warn
            logger.warning("⚠️  ADX computation failed — filter bypassed")
            return True, 0.0

        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not adx_col:
            return True, 0.0

        adx_val = float(adx_df[adx_col[0]].iloc[-1])

        if signal_type == "BREAKOUT":
            # Breakouts can fire even in compression — only block extreme ranging
            threshold = ADX_MIN_TREND - 5   # effectively 15
        else:
            threshold = ADX_MIN_TREND       # 20 for TREND and DIP

        trending = adx_val >= threshold
        level = "STRONG" if adx_val >= ADX_STRONG_TREND else ("WEAK" if trending else "RANGING")
        logger.info(f"📐 ADX={adx_val:.1f} [{level}] | threshold={threshold} | signal={signal_type}")
        return trending, adx_val

    except Exception as e:
        logger.warning(f"⚠️  ADX filter error: {e} — bypassing")
        return True, 0.0


# ─── 2. RSI MULTI-LENGTH CONSENSUS ───────────────────────────

def rsi_consensus(df: pd.DataFrame, signal: str) -> tuple[bool, dict]:
    """
    Computes RSI at lengths 5, 10, and 14. All three must agree on
    direction to pass. Returns (agrees: bool, rsi_values: dict).

    For a BUY signal: all three RSIs must be > 45
    For a SELL signal: all three RSIs must be < 55

    Args:
        df:     M5 DataFrame with close column
        signal: "BUY" or "SELL"

    Usage in xau.py:
        agrees, rsi_vals = rsi_consensus(df, signal)
        if not agrees:
            logger.info(f"⛔ RSI consensus failed: {rsi_vals}")
            signal = "NONE"
    """
    try:
        rsi_vals = {}
        for length in [5, 10, 14]:
            rsi_series = ta.rsi(df['close'], length=length)
            if rsi_series is not None and not rsi_series.empty:
                rsi_vals[f"rsi_{length}"] = round(float(rsi_series.iloc[-1]), 1)

        if len(rsi_vals) < 2:
            logger.warning("⚠️  RSI consensus: insufficient data — bypassing")
            return True, rsi_vals

        values = list(rsi_vals.values())

        if signal == "BUY":
            agrees = all(v > 45 for v in values)
        elif signal == "SELL":
            agrees = all(v < 55 for v in values)
        else:
            agrees = True

        logger.info(
            f"📊 RSI consensus [{signal}]: {rsi_vals} → "
            f"{'✅ AGREE' if agrees else '❌ DISAGREE'}"
        )
        return agrees, rsi_vals

    except Exception as e:
        logger.warning(f"⚠️  RSI consensus error: {e} — bypassing")
        return True, {}


# ─── 3. SIGNAL QUALITY SCORE ─────────────────────────────────

def signal_quality_score(
    df: pd.DataFrame,
    signal: str,
    signal_type: str,
    h1_trend: str,
    h4_trend: str,
    adx_val: float,
) -> int:
    """
    Returns an integer quality score 0–100.
    Used for logging and as an additional gate in ai_context.py.

    Scoring breakdown:
      ADX strength         : 0–30 pts
      H1 + H4 alignment    : 0–25 pts
      RSI position         : 0–20 pts
      Candle momentum      : 0–15 pts
      Signal type bonus    : 0–10 pts

    A score below 40 is considered low quality — log it, optionally skip.
    """
    score = 0
    curr = df.iloc[-1]

    # ADX component (0–30)
    if adx_val >= ADX_STRONG_TREND:
        score += 30
    elif adx_val >= ADX_MIN_TREND:
        score += 15
    else:
        score += 0

    # Trend alignment (0–25)
    if signal == "BUY":
        if h1_trend == "UP":   score += 12
        if h4_trend == "UP":   score += 13
    elif signal == "SELL":
        if h1_trend == "DOWN": score += 12
        if h4_trend == "DOWN": score += 13

    # RSI position (0–20)
    rsi = curr.get('RSI', 50)
    if signal == "BUY":
        if rsi > 55:   score += 20
        elif rsi > 48: score += 12
        elif rsi > 42: score += 5
    elif signal == "SELL":
        if rsi < 45:   score += 20
        elif rsi < 52: score += 12
        elif rsi < 58: score += 5

    # Candle momentum (0–15) — green candle on BUY, red on SELL
    candle_range = curr['high'] - curr['low']
    candle_body  = abs(curr['close'] - curr['open'])
    body_pct     = candle_body / candle_range if candle_range > 0 else 0
    if signal == "BUY"  and curr['close'] > curr['open'] and body_pct > 0.5: score += 15
    elif signal == "SELL" and curr['close'] < curr['open'] and body_pct > 0.5: score += 15
    elif body_pct > 0.3: score += 7

    # Signal type bonus (0–10)
    if signal_type == "BREAKOUT": score += 10
    elif signal_type == "DIP":    score += 5

    logger.info(f"🎯 Signal quality score: {score}/100 [{signal_type} {signal}]")
    return score


# ─── 4. CANDLE BODY FILTER ───────────────────────────────────

def candle_is_decisive(df: pd.DataFrame) -> bool:
    """
    Returns True if the current candle has a real body (not a doji).
    Blocks entries on indecision candles.

    A candle is decisive if its body is >= DOJI_BODY_PCT of its total range.
    """
    curr = df.iloc[-1]
    candle_range = curr['high'] - curr['low']
    if candle_range == 0:
        return False

    body_pct = abs(curr['close'] - curr['open']) / candle_range
    decisive = body_pct >= DOJI_BODY_PCT

    if not decisive:
        logger.info(
            f"⛔ Doji filter: body={body_pct:.1%} of range — candle is indecision, skipping"
        )
    return decisive
