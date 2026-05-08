import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import os
import csv
import json
import logging
import requests
import base64
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from datetime import datetime, timezone

# ─── LOGGING SETUP ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    handlers=[
        logging.FileHandler("scuro_fast_acc.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()
MT5_LOGIN    = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER   = os.getenv("MT5_SERVER")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ─── FIXED SETTINGS (never changed by advisor) ───────────────
SYMBOL           = "XAUUSDm"
TIMEFRAME        = mt5.TIMEFRAME_M5
MAGIC_NUMBER     = 777777
LOGIC_MODEL      = "llama-3.3-70b-versatile"
SECONDARY_MODEL  = "llama-3.1-8b-instant"
HISTORY_CSV      = "trade_history.csv"
SUMMARY_INTERVAL = 10
CONFIG_JSON      = "scuro_config.json"

# ─── ADAPTIVE CONFIG (hot-reloaded from advisor.py) ──────────
_ADAPTIVE = {
    "accumulation_lot":   0.02,
    "reward_ratio":       2.0,
    "rsi_buy_threshold":  48,
    "rsi_sell_threshold": 52,
    "sl_atr_mult":        1.8,
    "trail_atr_mult":     1.2,
    "ema_fast":           5,
    "ema_slow":           8,
    # ── NEW: Dip-buy settings ─────────────────────────────────
    # When H1 & H4 are both UP but price pulls below H1 EMA,
    # we look for RSI to recover above this threshold as the
    # "dip is done, time to buy" trigger.
    "dip_rsi_recovery":   38,   # RSI must cross back above this
    "dip_enabled":        True, # master switch for dip-buy logic
}
_last_config_mtime = 0.0
_last_trade_time = 0.0   # Unix timestamp of last trade placement

def load_adaptive_config():
    global _ADAPTIVE, _last_config_mtime
    if not os.path.exists(CONFIG_JSON):
        return
    try:
        mtime = os.path.getmtime(CONFIG_JSON)
        if mtime <= _last_config_mtime:
            return
        with open(CONFIG_JSON) as f:
            cfg = json.load(f)
        _last_config_mtime = mtime
        changed = []
        for key in _ADAPTIVE:
            if key in cfg and cfg[key] != _ADAPTIVE[key]:
                changed.append(f"{key}: {_ADAPTIVE[key]} → {cfg[key]}")
                _ADAPTIVE[key] = cfg[key]
        if changed:
            logger.info("🔄 ADVISOR UPDATE APPLIED:")
            for c in changed:
                logger.info(f"   ✦ {c}")
            reason = cfg.get("update_reason", "")
            if reason:
                logger.info(f"   💡 Reason: {reason}")
    except Exception as e:
        logger.warning(f"⚠️  Config reload error: {e}")

# ─── TRADE HISTORY ───────────────────────────────────────────
HISTORY_COLS = [
    "ticket", "open_time", "close_time", "symbol", "direction",
    "lot", "entry_price", "sl", "tp",
    "close_price", "profit", "outcome",
    "rsi", "h1_trend", "news", "ai_reason",
    "signal_type",   # NEW: tracks "TREND" vs "DIP" so we can analyse which works better
]

def _load_history() -> pd.DataFrame:
    if os.path.exists(HISTORY_CSV):
        df = pd.read_csv(HISTORY_CSV, dtype={"ticket": str})
        # Back-fill signal_type for old rows that don't have it
        if "signal_type" not in df.columns:
            df["signal_type"] = "TREND"
        return df
    return pd.DataFrame(columns=HISTORY_COLS)

def _save_history(df: pd.DataFrame):
    df.to_csv(HISTORY_CSV, index=False)

def _fmt_ts(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")

def sync_mt5_history():
    from datetime import timedelta
    date_from = datetime(2000, 1, 1)
    date_to   = datetime.now() + timedelta(days=1)

    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        logger.warning("⚠️  MT5 returned no deal history.")
        return

    entries: dict = {}
    exits:   dict = {}

    for d in deals:
        if d.symbol != SYMBOL or d.magic != MAGIC_NUMBER:
            continue
        pid = str(d.position_id)
        if d.entry == mt5.DEAL_ENTRY_IN:
            entries[pid] = d
        elif d.entry == mt5.DEAL_ENTRY_OUT:
            exits[pid] = d

    if not exits:
        return

    df              = _load_history()
    existing_tickets = set(df["ticket"].astype(str).tolist())
    new_rows        = []
    updated         = False

    for pid, out_deal in exits.items():
        in_deal  = entries.get(pid)
        ticket   = pid
        profit   = round(out_deal.profit, 2)
        outcome  = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BE")

        if ticket in existing_tickets:
            mask = (df["ticket"].astype(str) == ticket) & (df["outcome"] == "OPEN")
            if mask.any():
                df.loc[mask, "close_price"] = round(out_deal.price, 2)
                df.loc[mask, "profit"]      = profit
                df.loc[mask, "close_time"]  = _fmt_ts(out_deal.time)
                df.loc[mask, "outcome"]     = outcome
                logger.info(f"📊 Trade #{ticket} closed → {outcome} | P&L: {profit:+.2f}")
                updated = True
        else:
            direction   = "BUY"  if (in_deal and in_deal.type == mt5.DEAL_TYPE_BUY)  else "SELL"
            entry_price = round(in_deal.price, 2) if in_deal else round(out_deal.price, 2)
            open_time   = _fmt_ts(in_deal.time)  if in_deal else ""
            lot         = in_deal.volume          if in_deal else out_deal.volume

            sl, tp = "", ""
            orders = mt5.history_orders_get(position=int(pid))
            if orders:
                for o in orders:
                    if o.sl: sl = round(o.sl, 2)
                    if o.tp: tp = round(o.tp, 2)

            new_rows.append({
                "ticket":      ticket,
                "open_time":   open_time,
                "close_time":  _fmt_ts(out_deal.time),
                "symbol":      SYMBOL,
                "direction":   direction,
                "lot":         lot,
                "entry_price": entry_price,
                "sl":          sl,
                "tp":          tp,
                "close_price": round(out_deal.price, 2),
                "profit":      profit,
                "outcome":     outcome,
                "rsi":         "N/A",
                "h1_trend":    "N/A",
                "news":        "N/A",
                "ai_reason":   "historical",
                "signal_type": "HISTORICAL",
            })

    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
        df = df.sort_values("open_time", na_position="last").reset_index(drop=True)
        logger.info(f"📥 Synced {len(new_rows)} new historical trade(s) into {HISTORY_CSV}")
        updated = True

    if updated:
        _save_history(df)

def record_trade_open(ticket, signal, price, sl, tp, rsi, h1_trend, news, ai_reason, signal_type="TREND"):
    df  = _load_history()
    row = {
        "ticket":      str(ticket),
        "open_time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "close_time":  "",
        "symbol":      SYMBOL,
        "direction":   signal,
        "lot":         _ADAPTIVE["accumulation_lot"],
        "entry_price": round(price, 2),
        "sl":          round(sl, 2),
        "tp":          round(tp, 2),
        "close_price": "",
        "profit":      "",
        "outcome":     "OPEN",
        "rsi":         round(rsi, 1),
        "h1_trend":    h1_trend,
        "news":        news,
        "ai_reason":   ai_reason,
        "signal_type": signal_type,
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _save_history(df)
    logger.info(f"📋 Trade #{ticket} logged to {HISTORY_CSV} [{signal_type}]")

def print_summary_table():
    df = _load_history()
    if df.empty:
        logger.info("📋 No trade history yet.")
        return

    closed      = df[df["outcome"] != "OPEN"]
    wins        = len(closed[closed["outcome"] == "WIN"])
    losses      = len(closed[closed["outcome"] == "LOSS"])
    be          = len(closed[closed["outcome"] == "BE"])
    open_n      = len(df[df["outcome"] == "OPEN"])
    wr          = (wins / len(closed) * 100) if len(closed) > 0 else 0.0
    pnl_series  = pd.to_numeric(closed["profit"], errors="coerce").dropna()
    total_pnl   = pnl_series.sum()
    best_trade  = pnl_series.max() if not pnl_series.empty else 0
    worst_trade = pnl_series.min() if not pnl_series.empty else 0

    # ── NEW: Break down by signal type ───────────────────────
    if "signal_type" in closed.columns:
        dip_trades  = closed[closed["signal_type"] == "DIP"]
        trend_trades = closed[closed["signal_type"] == "TREND"]
        dip_pnl     = pd.to_numeric(dip_trades["profit"], errors="coerce").sum()
        trend_pnl   = pd.to_numeric(trend_trades["profit"], errors="coerce").sum()
        dip_wr      = (len(dip_trades[dip_trades["outcome"] == "WIN"]) / len(dip_trades) * 100) if len(dip_trades) > 0 else 0
        trend_wr    = (len(trend_trades[trend_trades["outcome"] == "WIN"]) / len(trend_trades) * 100) if len(trend_trades) > 0 else 0
    else:
        dip_pnl = trend_pnl = dip_wr = trend_wr = 0
        dip_trades = trend_trades = pd.DataFrame()

    account_info = mt5.account_info()
    balance = account_info.balance if account_info else "N/A"

    sep = "─" * 64
    logger.info(sep)
    logger.info("📊  SCURO TRADE HISTORY SUMMARY")
    logger.info(sep)
    logger.info(f"  Total Trades : {len(df)}  (Open: {open_n}  Closed: {len(closed)})")
    logger.info(f"  Results      : ✅ Wins: {wins}  ❌ Losses: {losses}  〰 BE: {be}")
    logger.info(f"  Win Rate     : {wr:.1f}%")
    logger.info(f"  Total P&L    : {total_pnl:+.2f} KES")
    logger.info(f"  Best Trade   : {best_trade:+.2f} KES")
    logger.info(f"  Worst Trade  : {worst_trade:+.2f} KES")
    logger.info(f"  💰 Balance    : {balance:,.2f} KES" if balance != "N/A" else f"  💰 Balance    : {balance}")
    logger.info(sep)
    # Signal type breakdown
    logger.info(f"  📈 TREND signals : {len(trend_trades)} trades | WR: {trend_wr:.1f}% | P&L: {trend_pnl:+.2f}")
    logger.info(f"  📉 DIP signals   : {len(dip_trades)} trades | WR: {dip_wr:.1f}% | P&L: {dip_pnl:+.2f}")
    logger.info(sep)

    recent = df.tail(10)
    logger.info("  LAST 10 TRADES:")
    logger.info(f"  {'Opened':<20} {'Dir':<5} {'Type':<6} {'Entry':>8} {'SL':>8} "
                f"{'TP':>8} {'Close':>8} {'P&L':>7}  Result")
    logger.info("  " + "·" * 70)
    for _, r in recent.iterrows():
        try:
            pnl_str = f"{float(r['profit']):+.2f}"
        except (ValueError, TypeError):
            pnl_str = "  OPEN"
        close_str = str(r['close_price']) if r['close_price'] != "" else "  —"
        stype = str(r.get('signal_type', 'TREND'))[:5]
        logger.info(
            f"  {str(r['open_time']):<20} {str(r['direction']):<5} "
            f"{stype:<6} "
            f"{float(r['entry_price']):>8.2f} {float(r['sl']):>8.2f} "
            f"{float(r['tp']):>8.2f} {close_str:>8} {pnl_str:>7}  {r['outcome']}"
        )
    logger.info(sep)

# ─── RATE LIMITER ────────────────────────────────────────────
class GroqLimiter:
    def __init__(self, rpm):
        self.interval = 60.0 / rpm
        self.last_call = 0.0
    def wait(self):
        elapsed = time.time() - self.last_call
        if (gap := self.interval - elapsed) > 0: time.sleep(gap)
        self.last_call = time.time()

_limiter = GroqLimiter(20)

# ─── TOKEN BUDGETER ──────────────────────────────────────────
import re as _re
from datetime import date as _date

class TokenBudgeter:
    DAILY_LIMIT      = 100_000
    SAFETY_BUFFER    = 10_000
    TOKENS_PER_CALL  = 350

    def __init__(self):
        self.tokens_used  = 0
        self.calls_today  = 0
        self.last_reset   = _date.today()
        self._rate_limited_until = 0.0

    def _maybe_reset(self):
        today = _date.today()
        if today != self.last_reset:
            logger.info(f"🌅 New day — resetting token budget (used {self.tokens_used:,} yesterday)")
            self.tokens_used = 0
            self.calls_today = 0
            self.last_reset  = today
            self._rate_limited_until = 0.0

    def budget_remaining(self) -> int:
        self._maybe_reset()
        return self.DAILY_LIMIT - self.tokens_used

    def can_call_ai(self) -> bool:
        self._maybe_reset()
        if time.time() < self._rate_limited_until:
            return False
        return self.budget_remaining() > self.SAFETY_BUFFER

    def record_call(self, tokens: int = None):
        self._maybe_reset()
        spent = tokens if tokens else self.TOKENS_PER_CALL
        self.tokens_used += spent
        self.calls_today += 1

    def parse_and_sleep_rate_limit(self, error_message: str) -> float:
        match = _re.search(
            r'try again in\s+(?:(\d+)m\s*)?(\d+(?:\.\d+)?s)?',
            error_message, _re.IGNORECASE
        )
        if not match:
            wait = 60.0
        else:
            minutes = int(match.group(1)) if match.group(1) else 0
            seconds = float(match.group(2).rstrip('s')) if match.group(2) else 0.0
            wait = minutes * 60 + seconds

        wait = max(wait, 10.0)
        self._rate_limited_until = time.time() + wait
        logger.warning(
            f"⏳ Rate limit hit — sleeping {wait:.0f}s "
            f"(budget remaining: {self.budget_remaining():,} tokens)"
        )
        time.sleep(wait)
        return wait

    def status_line(self) -> str:
        self._maybe_reset()
        pct = self.tokens_used / self.DAILY_LIMIT * 100
        return (f"🪙 Tokens: {self.tokens_used:,}/{self.DAILY_LIMIT:,} "
                f"({pct:.0f}% used) | AI calls today: {self.calls_today}")

_budgeter = TokenBudgeter()

# ─── CORE UTILITIES ──────────────────────────────────────────

def get_h1_context():
    """Returns H1 trend direction AND the current H1 EMA50 price level."""
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
    if rates is None: return "UNKNOWN", None
    df    = pd.DataFrame(rates)
    ema50 = ta.ema(df['close'], length=50).iloc[-1]
    trend = "UP" if df['close'].iloc[-1] > ema50 else "DOWN"
    return trend, ema50   # ← NOW returns the EMA price too

def get_h4_context():
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H4, 0, 50)
    if rates is None: return "UNKNOWN"
    df    = pd.DataFrame(rates)
    ema50 = ta.ema(df['close'], length=50).iloc[-1]
    return "UP" if df['close'].iloc[-1] > ema50 else "DOWN"

def generate_chart_text_summary(df: pd.DataFrame, last_n: int = 7) -> str:
    recent = df.tail(last_n).reset_index(drop=True)
    if len(recent) == 0:
        return "No data"
    lines = []
    for _, row in recent.iterrows():
        direction = "UP" if row['close'] > row['open'] else "DN"
        lines.append(
            f"C:{row['close']:.2f} RSI:{row.get('RSI',0):.0f} "
            f"ATR:{row.get('ATR',0):.2f} {direction}"
        )
    curr = recent.iloc[-1]
    trend = "BULL" if curr.get('EMA_5', 0) > curr.get('EMA_8', 0) else "BEAR"
    lines.append(f"NOW:{curr['close']:.2f} RSI:{curr.get('RSI',0):.1f} {trend}")
    return " | ".join(lines)

def fetch_news():
    try:
        url  = "https://www.forexfactory.com/ff_calendar_thisweek.xml"
        r    = requests.get(url, timeout=10)
        root = ET.fromstring(r.content)
        events = [item.find('title').text for item in root.findall('event')
                  if item.find('impact').text in ['High', 'Medium']]
        return " | ".join(events[:2]) if events else "Quiet"
    except: return "Offline"

def _groq_chat(headers: dict, model: str, prompt: str, timeout: int = 15) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 50},
        timeout=timeout,
    )
    data = resp.json()

    if "choices" not in data:
        err      = data.get("error", {})
        err_type = err.get("type", "unknown")
        err_msg  = err.get("message", str(data))
        if err_type in ("tokens", "rate_limit_exceeded") or "rate limit" in err_msg.lower():
            _budgeter.parse_and_sleep_rate_limit(err_msg)
        raise Exception(f"Groq [{err_type}]: {err_msg}")

    _budgeter.record_call()
    return data["choices"][0]["message"]["content"].strip().upper()


def _rule_based_confirm(signal: str, rsi: float, h1_trend: str, h4_trend: str,
                        signal_type: str = "TREND") -> bool:
    """
    Pure math fallback when AI budget is exhausted.
    Now handles both TREND and DIP signal types.
    """
    if signal_type == "DIP":
        # For dip buys: both higher timeframes must be UP, RSI recovering
        return h1_trend == "UP" and h4_trend == "UP" and rsi > _ADAPTIVE.get("dip_rsi_recovery", 38)
    if signal == "BUY":
        return h1_trend == "UP" and h4_trend == "UP" and rsi > 45
    elif signal == "SELL":
        return h1_trend == "DOWN" and h4_trend == "DOWN" and rsi < 55
    return False


def get_dual_ai_consensus(signal, rsi, atr, h1_trend, h4_trend, news,
                          chart_text="", signal_type="TREND"):
    if not _budgeter.can_call_ai():
        confirmed = _rule_based_confirm(signal, rsi, h1_trend, h4_trend, signal_type)
        remaining = _budgeter.budget_remaining()
        logger.warning(
            f"⚠️  AI budget low ({remaining:,} tokens left) — "
            f"rule-based decision: {'CONFIRM' if confirmed else 'REJECT'}"
        )
        tag = "RULE" if confirmed else "RULE-NO"
        return confirmed, f"L:{tag} S:SKIP"

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    _limiter.wait()
    try:
        # Tell the AI which type of signal this is so it judges it correctly
        type_note = (
            "This is a DIP-BUY: trend is UP on H1+H4 but price pulled back. "
            "Confirm if the dip looks done and momentum is recovering."
            if signal_type == "DIP"
            else "Standard trend-following signal."
        )
        l_prompt = (
            f"{SYMBOL} {signal} | RSI:{rsi:.1f} H1:{h1_trend} H4:{h4_trend} News:{news}\n"
            f"Chart: {chart_text}\n"
            f"{type_note}\n"
            f"Reply CONFIRM or REJECT only."
        )
        l_res = _groq_chat(headers, LOGIC_MODEL, l_prompt)
    except Exception as e:
        logger.error(f"❌ Logic agent error: {e}")
        confirmed = _rule_based_confirm(signal, rsi, h1_trend, h4_trend, signal_type)
        logger.info(f"🔄 Fallback rule decision: {'CONFIRM' if confirmed else 'REJECT'}")
        return confirmed, f"L:FALLBK S:SKIP"

    v_res = "CONFIRM"
    _limiter.wait()
    try:
        v_prompt = f"{signal} on {SYMBOL}? {chart_text} [{signal_type}] Reply CONFIRM or REJECT only."
        v_res = _groq_chat(headers, SECONDARY_MODEL, v_prompt)
        logger.info(f"✅ Both agents OK | {_budgeter.status_line()}")
    except Exception as e:
        logger.warning(f"⚠️  Secondary check failed: {e} — logic agent decides alone")

    return ("CONFIRM" in l_res and "CONFIRM" in v_res), f"L:{l_res[:8]} S:{v_res[:8]}"


# ─── NEW: DIP DETECTION ──────────────────────────────────────
# This is the fix for the "frozen bot" gap identified from the charts.
#
# WHAT IT DOES IN PLAIN ENGLISH:
#   Imagine you're watching gold on the big picture (H4, H1) — the
#   trend is clearly going UP. But right now on the 5-minute chart,
#   price has pulled back and the short EMAs have crossed down.
#   The bot's normal BUY rule would miss this because it requires the
#   short EMAs to be going UP on M5.
#
#   This function detects exactly that situation:
#   "Big trend = UP, short-term = pulling back, RSI suggesting
#    the dip might be over" → flag it as a DIP_BUY opportunity.
#
# SAFETY CHECKS:
#   1. Only fires when H1 AND H4 are both trending UP (gold must be
#      in a genuine uptrend on multiple timeframes)
#   2. Price must have actually fallen BELOW the H1 EMA50 (real dip,
#      not just a tiny wobble)
#   3. RSI on M5 must have recovered above dip_rsi_recovery (38 by
#      default) — this filters out dips that are still falling
#   4. Current M5 candle must close UP (green candle = buyers stepping in)
#   5. We count how many candles price has been below H1 EMA and
#      require at least 2 (avoids false positives on brief spikes)
#   6. Checks existing open positions to avoid over-stacking

_dip_candles_below_h1: int = 0   # rolling counter — resets when price is above H1 EMA

def check_dip_buy_signal(curr, prev, df, h1_trend: str, h4_trend: str,
                          h1_ema_price: float) -> bool:
    """
    Returns True when all dip-buy conditions are met.
    Updates the global counter _dip_candles_below_h1.
    """
    global _dip_candles_below_h1

    # Master switch
    if not _ADAPTIVE.get("dip_enabled", True):
        return False

    # Condition 1: Both higher timeframes bullish
    if h1_trend != "UP" or h4_trend != "UP":
        _dip_candles_below_h1 = 0
        return False

    # H1 EMA price must be valid
    if h1_ema_price is None:
        return False

    current_price = curr['close']

    # Condition 2: Price is below the H1 EMA (we're in a dip)
    if current_price >= h1_ema_price:
        _dip_candles_below_h1 = 0   # back above EMA — reset counter
        return False

    # Count how many consecutive M5 candles have been below H1 EMA
    _dip_candles_below_h1 += 1

    # Condition 3: RSI recovering (above the dip threshold, not still falling)
    rsi = curr.get('RSI', 50)
    if rsi < _ADAPTIVE.get("dip_rsi_recovery", 38):
        return False

    # Condition 4: Current candle is a green/bullish candle (buyers returning)
    if curr['close'] <= curr['open']:
        return False

    # Condition 5: Dip must have lasted at least 2 candles (real pullback, not a spike)
    if _dip_candles_below_h1 < 2:
        return False

    # Condition 6: RSI must be higher than the previous candle (momentum turning up)
    prev_rsi = prev.get('RSI', 50)
    if rsi <= prev_rsi:
        return False

    # Condition 7: Don't stack too many dip trades — max 1 open dip position
    open_positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if open_positions and len(open_positions) >= 2:
        return False

    logger.info(
        f"🎯 DIP DETECTED: Price {current_price:.2f} below H1 EMA ({h1_ema_price:.2f}) "
        f"| RSI recovering: {prev_rsi:.1f}→{rsi:.1f} "
        f"| Candles below EMA: {_dip_candles_below_h1}"
    )
    return True


# ─── TRADE MANAGEMENT ────────────────────────────────────────

def lock_profitable_trades():
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions: return
    for pos in positions:
        point = mt5.symbol_info(SYMBOL).point
        profit_pts = (pos.price_current - pos.price_open) / point if pos.type == 0 else (pos.price_open - pos.price_current) / point
        if profit_pts > 100 and pos.sl != pos.price_open:
            new_sl = pos.price_open + (10 * point) if pos.type == 0 else pos.price_open - (10 * point)
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": new_sl, "tp": pos.tp})

def trail_stops():
    """Milestone Trail: Locks in profit in steps of 1,000."""
    STEP = 1000
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions: return

    for pos in positions:
        if pos.profit < STEP:
            continue

        num_steps    = int(pos.profit // STEP)
        lock_amount  = (num_steps - 1) * STEP
        symbol_info  = mt5.symbol_info(SYMBOL)
        price_offset = lock_amount / (pos.volume * symbol_info.trade_contract_size)

        if pos.type == mt5.POSITION_TYPE_BUY:
            target_sl = pos.price_open + price_offset
            if target_sl > pos.sl + (10 * symbol_info.point):
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl": target_sl,
                    "tp": pos.tp
                })
        elif pos.type == mt5.POSITION_TYPE_SELL:
            target_sl = pos.price_open - price_offset
            if pos.sl == 0 or target_sl < pos.sl - (10 * symbol_info.point):
                mt5.order_send({
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl": target_sl,
                    "tp": pos.tp
                })

def close_on_profit_target(target=1000):
    """Surgical exit: Closes any trade that hits the 1,000 KES profit mark."""
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions:
        return

    for pos in positions:
        if pos.profit >= target:
            tick = mt5.symbol_info_tick(SYMBOL)
            type_close  = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price_close = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "position":     pos.ticket,
                "symbol":       SYMBOL,
                "volume":       pos.volume,
                "type":         type_close,
                "price":        price_close,
                "magic":        MAGIC_NUMBER,
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(request)
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"💰 TARGET REACHED: Closed Ticket {pos.ticket} at {pos.profit:.2f} profit.")
            else:
                logger.error(f"❌ Target Close Failed: {res.comment}")


# ─── MAIN ENGINE ─────────────────────────────────────────────

def run_bot():
    global _last_trade_time
    
    if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
        logger.error("❌ MT5 Init Failed")
        return

    logger.info("=" * 64)
    logger.info("🔥 SCURO ACCUMULATOR V3: DIP-HUNTER EDITION")
    logger.info(f"   Symbol: {SYMBOL} | Lot: {_ADAPTIVE['accumulation_lot']} | Risk: Aggressive")
    logger.info(f"   History file: {os.path.abspath(HISTORY_CSV)}")
    logger.info(f"   Dip-buy logic: {'ENABLED ✅' if _ADAPTIVE.get('dip_enabled') else 'DISABLED ❌'}")
    logger.info("=" * 64)

    logger.info("📥 Syncing full MT5 trade history...")
    sync_mt5_history()
    print_summary_table()

    last_min     = -1
    last_summary = -1

    while True:
        now   = datetime.now()
        rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, 100)
        if rates is None:
            time.sleep(1)
            continue

        df = pd.DataFrame(rates)
        df['EMA_5'] = ta.ema(df['close'], int(_ADAPTIVE['ema_fast']))
        df['EMA_8'] = ta.ema(df['close'], int(_ADAPTIVE['ema_slow']))
        df['RSI']   = ta.rsi(df['close'], 14)
        df['ATR']   = ta.atr(df['high'], df['low'], df['close'], 14)

        curr, prev = df.iloc[-1], df.iloc[-2]

        close_on_profit_target(2500)  # Increased from 1000 to match 2,500+ KES loss potential
        trail_stops()
        sync_mt5_history()

        summary_slot = (now.hour * 60 + now.minute) // SUMMARY_INTERVAL
        if summary_slot != last_summary:
            print_summary_table()
            last_summary = summary_slot

        if now.minute != last_min:
            load_adaptive_config()

            # get_h1_context now returns BOTH trend direction AND EMA price
            h1_trend, h1_ema_price = get_h1_context()
            h4_trend = get_h4_context()

            logger.info(
                f"🔍 Scan: {SYMBOL} | Price: {curr['close']:.2f} | "
                f"RSI: {curr['RSI']:.1f} | H1: {h1_trend} (EMA@{h1_ema_price:.2f} if known) | "
                f"H4: {h4_trend} | {_budgeter.status_line()}"
            )

            signal      = "NONE"
            signal_type = "TREND"

            # ── Standard trend signals ────────────────────────
            if curr['EMA_5'] > curr['EMA_8'] and curr['RSI'] > _ADAPTIVE['rsi_buy_threshold']:
                signal = "BUY"
                signal_type = "TREND"

            elif (curr['EMA_5'] < curr['EMA_8']
                  and curr['RSI'] < _ADAPTIVE['rsi_sell_threshold']
                  and h4_trend == "DOWN"):
                signal = "SELL"
                signal_type = "TREND"

            elif curr['EMA_5'] < curr['EMA_8'] and curr['RSI'] < _ADAPTIVE['rsi_sell_threshold']:
                logger.info(f"⛔ SELL signal suppressed — H4 is {h4_trend} (need DOWN)")

            # ── Cooldown gate: Don't fire new trades too frequently ──
            mins_since_last_trade = (time.time() - _last_trade_time) / 60
            cooldown_active = (_last_trade_time > 0) and (mins_since_last_trade < _ADAPTIVE.get('cooldown_mins', 30))
            
            if cooldown_active:
                logger.info(f"⏱️  Cooldown active: {_ADAPTIVE.get('cooldown_mins', 30) - mins_since_last_trade:.1f}m remaining")
            else:
                # ── NEW: Dip-buy check (runs even when standard signals are NONE) ──
                # In plain English: even if the 5-min chart looks bearish,
                # check if we're in a golden "buy the pullback" scenario.
                if signal == "NONE":
                    if check_dip_buy_signal(curr, prev, df, h1_trend, h4_trend, h1_ema_price):
                        signal      = "BUY"
                        signal_type = "DIP"
                        logger.info("💧 Dip-buy signal activated — price pulled back into H1 uptrend zone")

            if signal != "NONE" and not cooldown_active:
                news       = fetch_news()
                chart_text = generate_chart_text_summary(df)
                logger.info(f"📡 {signal} signal [{signal_type}] found. AI analysis...")
                ok, reason = get_dual_ai_consensus(
                    signal, curr['RSI'], curr['ATR'], h1_trend, h4_trend,
                    news, chart_text, signal_type=signal_type
                )

                if ok:
                    # ── MAX OPEN POSITIONS CHECK ──
                    open_positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
                    open_count = len(open_positions) if open_positions else 0
                    max_allowed = int(_ADAPTIVE.get('max_open_positions', 1))
                    if open_count >= max_allowed:
                        logger.info(f"📊 Max positions reached ({open_count}/{max_allowed}) — skipping trade")
                    else:
                        term_info = mt5.terminal_info()
                        if term_info and not term_info.trade_allowed:
                            logger.error(
                                "❌ AutoTrading DISABLED — click [AutoTrading] in MT5 toolbar."
                            )
                            last_min = now.minute
                            time.sleep(1)
                            continue

                        tick    = mt5.symbol_info_tick(SYMBOL)
                        price   = tick.ask if signal == "BUY" else tick.bid

                        # ── DIP trades get a tighter SL (we're counter-momentum on M5)
                        # Standard trades use the normal ATR multiplier.
                        sl_mult = (
                            _ADAPTIVE['sl_atr_mult'] * 0.8   # 20% tighter SL for dip entries
                            if signal_type == "DIP"
                            else _ADAPTIVE['sl_atr_mult']
                        )
                        sl_dist = curr['ATR'] * sl_mult
                        sl      = price - sl_dist if signal == "BUY" else price + sl_dist
                        tp      = price + (sl_dist * _ADAPTIVE['reward_ratio']) if signal == "BUY" else price - (sl_dist * _ADAPTIVE['reward_ratio'])

                        req = {
                            "action":       mt5.TRADE_ACTION_DEAL,
                            "symbol":       SYMBOL,
                            "volume":       _ADAPTIVE["accumulation_lot"],
                            "type":         mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL,
                            "price":        price,
                            "sl":           sl,
                            "tp":           tp,
                            "magic":        MAGIC_NUMBER,
                            "type_time":    mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_IOC,
                        }
                        res = mt5.order_send(req)

                        if res.retcode == mt5.TRADE_RETCODE_DONE:
                            _last_trade_time = time.time()
                            logger.info(f"✅ TRADE PLACED: {signal} [{signal_type}] @ {price:.2f} | AI: {reason}")
                            record_trade_open(
                                ticket=res.order, signal=signal,
                                price=price, sl=sl, tp=tp,
                                rsi=curr['RSI'], h1_trend=h1_trend,
                                news=news, ai_reason=reason,
                                signal_type=signal_type,
                            )
                        else:
                            logger.error(f"❌ Execution Error: {res.comment}")
                else:
                    logger.info(f"⏭️  Rejected by AI: {reason}")

            last_min = now.minute
        time.sleep(1)

if __name__ == "__main__":
    run_bot()
