"""
xau.py  ─  Scuro XAUUSD Scalper (Rebuilt)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Key changes from previous version:
  • LLM confirmation gate REMOVED — replaced with pure technical_confirmation()
    Zero API calls per trade. Zero rate limits. Zero latency. Never shoots blind.
  • advisor.py handles all LLM work (once per 30 min) and tunes the filters.
  • sync_mt5_history() moved OUT of the main tick loop → called once per minute only
  • Hot-reload includes max_open_positions and cooldown_mins (advisor can control these)
  • Daily loss circuit breaker: stops trading if daily P&L drops below DAILY_LOSS_LIMIT
  • Consecutive loss circuit breaker: pauses after N consecutive losses
  • News blackout: no trades within 15 min of high-impact events
  • Stale signal filter: no re-entering an EMA cross that is 2+ candles old
"""

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import os
import json
import logging
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from datetime import datetime, timedelta

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
# NOTE: No GROQ_API_KEY needed in xau.py. The LLM gate has been removed.
# Signal quality is enforced by tight technical filters + advisor-tuned parameters.
# The only LLM in the system is advisor.py (runs every 30 min, tunes the filters).

# ─── FIXED SETTINGS (never changed by advisor) ───────────────
SYMBOL           = "XAUUSDm"
TIMEFRAME        = mt5.TIMEFRAME_M5
MAGIC_NUMBER     = 777777
HISTORY_CSV      = "trade_history.csv"
SUMMARY_INTERVAL = 10
CONFIG_JSON      = "scuro_config.json"

# Hard circuit breakers — advisor cannot override these
DAILY_LOSS_LIMIT      = -500.0   # stop ALL trading if day's P&L drops below this (USD)
MAX_CONSECUTIVE_LOSSES = 6       # pause trading after this many losses in a row

# ─── ADAPTIVE CONFIG (hot-reloaded from advisor.py) ──────────
_ADAPTIVE = {
    "accumulation_lot":   0.02,
    "reward_ratio":       2.5,
    "rsi_buy_threshold":  45,
    "rsi_sell_threshold": 55,
    "sl_atr_mult":        1.8,
    "trail_atr_mult":     1.4,
    "ema_fast":           5,
    "ema_slow":           13,   # advisor floor enforced in load_adaptive_config()
    "max_open_positions": 2,
    "cooldown_mins":      15,
    "circuit_breaker_daily_loss_enabled": True,
    "circuit_breaker_consecutive_losses_enabled": True,
}

# Hard floors the advisor is NOT allowed to breach (enforced on hot-reload)
_CONFIG_HARD_FLOORS = {
    "sl_atr_mult":  1.4,   # below this stops get hunted on gold
    "ema_slow_min_gap": 5, # ema_slow must be at least ema_fast + this
}
_last_config_mtime  = 0.0
_last_trade_time    = 0.0   # Unix timestamp of last trade placement

def load_adaptive_config():
    """Hot-reload scuro_config.json if it has changed on disk. Called once per minute."""
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
        changed  = []
        rejected = []
        for key in _ADAPTIVE:
            if key not in cfg or cfg[key] == _ADAPTIVE[key]:
                continue
            new_val = cfg[key]

            # ── Hard floor: sl_atr_mult must stay >= 1.4 on gold ──
            if key == "sl_atr_mult" and float(new_val) < _CONFIG_HARD_FLOORS["sl_atr_mult"]:
                rejected.append(
                    f"{key}: {new_val} rejected (floor={_CONFIG_HARD_FLOORS['sl_atr_mult']}) — keeping {_ADAPTIVE[key]}"
                )
                continue

            # ── Hard rule: ema_slow must be at least ema_fast + 5 ──
            if key == "ema_slow":
                min_slow = int(_ADAPTIVE["ema_fast"]) + _CONFIG_HARD_FLOORS["ema_slow_min_gap"]
                if int(new_val) < min_slow:
                    rejected.append(
                        f"ema_slow: {new_val} rejected (need >= ema_fast+5={min_slow}) — keeping {_ADAPTIVE['ema_slow']}"
                    )
                    continue
            if key == "ema_fast":
                max_fast = int(_ADAPTIVE["ema_slow"]) - _CONFIG_HARD_FLOORS["ema_slow_min_gap"]
                if int(new_val) > max_fast:
                    rejected.append(
                        f"ema_fast: {new_val} rejected (need <= ema_slow-5={max_fast}) — keeping {_ADAPTIVE['ema_fast']}"
                    )
                    continue

            changed.append(f"{key}: {_ADAPTIVE[key]} → {new_val}")
            _ADAPTIVE[key] = new_val

        if changed:
            logger.info("🔄 ADVISOR UPDATE APPLIED:")
            for c in changed:
                logger.info(f"   ★ {c}")
            reason = cfg.get("update_reason", "")
            if reason:
                logger.info(f"   💡 Reason: {reason}")
        for r in rejected:
            logger.warning(f"   🛡️  CONFIG GUARD blocked: {r}")
    except Exception as e:
        logger.warning(f"⚠️  Config reload error: {e}")

# ─── CIRCUIT BREAKERS ────────────────────────────────────────

def check_daily_loss_circuit_breaker() -> bool:
    """
    Returns True (HALT) if today's closed P&L from MT5 history is
    below DAILY_LOSS_LIMIT. Checks robot trades only (MAGIC_NUMBER).
    """
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow    = today_start + timedelta(days=1)
    deals = mt5.history_deals_get(today_start, tomorrow)
    if not deals:
        return False

    today_pnl = sum(
        d.profit for d in deals
        if d.symbol == SYMBOL
        and d.magic == MAGIC_NUMBER
        and d.entry == mt5.DEAL_ENTRY_OUT
    )

    if today_pnl <= DAILY_LOSS_LIMIT:
        logger.warning(
            f"🛑 CIRCUIT BREAKER: Daily P&L = {today_pnl:+.2f} "
            f"(limit: {DAILY_LOSS_LIMIT:+.2f}) — trading halted for today"
        )
        return True
    return False

def check_consecutive_loss_circuit_breaker() -> bool:
    """
    Returns True (HALT) if the last N closed robot trades are all losses.
    Looks at the most recent MAX_CONSECUTIVE_LOSSES deals.
    """
    date_from = datetime.now() - timedelta(days=7)
    date_to   = datetime.now() + timedelta(days=1)
    deals = mt5.history_deals_get(date_from, date_to)
    if not deals:
        return False

    # Filter: closed robot trades only, newest first
    closed = [
        d for d in deals
        if d.symbol == SYMBOL
        and d.magic == MAGIC_NUMBER
        and d.entry == mt5.DEAL_ENTRY_OUT
    ]
    closed.sort(key=lambda d: d.time, reverse=True)

    recent = closed[:MAX_CONSECUTIVE_LOSSES]
    if len(recent) < MAX_CONSECUTIVE_LOSSES:
        return False

    all_losses = all(d.profit < 0 for d in recent)
    if all_losses:
        logger.warning(
            f"🛑 CIRCUIT BREAKER: Last {MAX_CONSECUTIVE_LOSSES} trades all losses — "
            f"pausing for {_ADAPTIVE['cooldown_mins']} minutes"
        )
        return True
    return False

def count_consecutive_losses_current() -> int:
    """Return the current active losing streak (for logging)."""
    date_from = datetime.now() - timedelta(days=7)
    date_to   = datetime.now() + timedelta(days=1)
    deals = mt5.history_deals_get(date_from, date_to)
    if not deals:
        return 0
    closed = sorted(
        [d for d in deals if d.symbol == SYMBOL and d.magic == MAGIC_NUMBER and d.entry == mt5.DEAL_ENTRY_OUT],
        key=lambda d: d.time, reverse=True
    )
    streak = 0
    for d in closed:
        if d.profit < 0:
            streak += 1
        else:
            break
    return streak

# ─── TRADE HISTORY (CSV) ─────────────────────────────────────
# The CSV is used for logging context (RSI, H1, AI reason) that MT5
# doesn't store natively. advisor.py reads from MT5 directly now,
# so the CSV is just a diagnostic log.

HISTORY_COLS = [
    "ticket", "open_time", "close_time", "symbol", "direction",
    "lot", "entry_price", "sl", "tp",
    "close_price", "profit", "outcome",
    "rsi", "h1_trend", "news", "ai_reason",
]

def _load_history() -> pd.DataFrame:
    if os.path.exists(HISTORY_CSV):
        return pd.read_csv(HISTORY_CSV, dtype={"ticket": str})
    return pd.DataFrame(columns=HISTORY_COLS)

def _save_history(df: pd.DataFrame):
    df.to_csv(HISTORY_CSV, index=False)

def _fmt_ts(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")

def sync_mt5_history():
    """
    Pull closed MT5 deals for SYMBOL and upsert into trade_history.csv.
    Called ONCE PER MINUTE (not on every tick) to avoid performance issues.
    """
    date_from = datetime(2000, 1, 1)
    date_to   = datetime.now() + timedelta(days=1)

    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
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

    df               = _load_history()
    existing_tickets = set(df["ticket"].astype(str).tolist())
    new_rows         = []
    updated          = False

    for pid, out_deal in exits.items():
        in_deal = entries.get(pid)
        ticket  = pid
        profit  = round(out_deal.profit, 2)
        outcome = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BE")

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
            direction   = "BUY"  if (in_deal and in_deal.type == mt5.DEAL_TYPE_BUY) else "SELL"
            entry_price = round(in_deal.price, 2) if in_deal else round(out_deal.price, 2)
            open_time   = _fmt_ts(in_deal.time) if in_deal else ""
            lot         = in_deal.volume if in_deal else out_deal.volume

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
            })

    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
        df = df.sort_values("open_time", na_position="last").reset_index(drop=True)
        logger.info(f"📥 Synced {len(new_rows)} new trade(s) into {HISTORY_CSV}")
        updated = True

    if updated:
        _save_history(df)

def record_trade_open(ticket, signal, price, sl, tp, rsi, h1_trend, news, ai_reason):
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
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _save_history(df)
    logger.info(f"📋 Trade #{ticket} logged to {HISTORY_CSV}")

def print_summary_table():
    df = _load_history()
    if df.empty:
        logger.info("📋 No trade history yet.")
        return

    closed     = df[df["outcome"] != "OPEN"]
    wins       = len(closed[closed["outcome"] == "WIN"])
    losses     = len(closed[closed["outcome"] == "LOSS"])
    be         = len(closed[closed["outcome"] == "BE"])
    open_n     = len(df[df["outcome"] == "OPEN"])
    wr         = (wins / len(closed) * 100) if len(closed) > 0 else 0.0
    pnl_series = pd.to_numeric(closed["profit"], errors="coerce").dropna()
    total_pnl  = pnl_series.sum()
    pf         = round(pnl_series[pnl_series > 0].sum() / abs(pnl_series[pnl_series < 0].sum()), 2) \
                 if abs(pnl_series[pnl_series < 0].sum()) > 0 else 999

    acct    = mt5.account_info()
    balance = acct.balance if acct else "N/A"
    streak  = count_consecutive_losses_current()

    sep = "─" * 64
    logger.info(sep)
    logger.info("📊  SCURO TRADE SUMMARY")
    logger.info(sep)
    logger.info(f"  Trades : {len(df)}  (Open: {open_n}  Closed: {len(closed)})")
    logger.info(f"  Results: ✅ {wins}W  ❌ {losses}L  〰 {be}BE  |  WR: {wr:.1f}%  PF: {pf}")
    logger.info(f"  P&L    : {total_pnl:+.2f} | Loss streak: {streak}")
    logger.info(f"  Balance: {balance:,.2f}" if balance != "N/A" else f"  Balance: {balance}")
    logger.info(f"  Config : lot={_ADAPTIVE['accumulation_lot']} rr={_ADAPTIVE['reward_ratio']} "
                f"rsi={_ADAPTIVE['rsi_buy_threshold']}/{_ADAPTIVE['rsi_sell_threshold']} "
                f"cool={_ADAPTIVE['cooldown_mins']}m max_pos={_ADAPTIVE['max_open_positions']}")
    logger.info(sep)

    recent = df.tail(10)
    logger.info("  LAST 10 TRADES:")
    logger.info(f"  {'Opened':<20} {'Dir':<5} {'Entry':>8} {'SL':>8} {'TP':>8} {'Close':>8} {'P&L':>7}  Result")
    logger.info("  " + "·" * 62)
    for _, r in recent.iterrows():
        try:
            pnl_str = f"{float(r['profit']):+.2f}"
        except (ValueError, TypeError):
            pnl_str = "  OPEN"
        close_str = str(r['close_price']) if r['close_price'] != "" else "  —"
        logger.info(
            f"  {str(r['open_time']):<20} {str(r['direction']):<5} "
            f"{float(r['entry_price']):>8.2f} "
            f"{str(r['sl']):>8} {str(r['tp']):>8} "
            f"{close_str:>8} {pnl_str:>7}  {r['outcome']}"
        )
    logger.info(sep)

# ─── CORE UTILITIES ──────────────────────────────────────────

def get_h1_context() -> str:
    """Returns H1 trend direction based on price vs EMA-50."""
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
    if rates is None:
        return "UNKNOWN"
    df    = pd.DataFrame(rates)
    ema50 = ta.ema(df['close'], length=50).iloc[-1]
    return "UP" if df['close'].iloc[-1] > ema50 else "DOWN"

# ─── NEWS CACHE ──────────────────────────────────────────────
# Economic calendars don't change every minute.
# We fetch once every 30 min and cache the result so every source
# only gets ~2 requests/hour instead of 60 — no more 429s.
#
# Source waterfall (tried in order, first success wins):
#   1. Finnhub  — proper financial API, free tier, very reliable.
#      Add FINNHUB_KEY=your_key to your .env (free at finnhub.io).
#   2. nfs.faireconomy.media — ForexFactory mirror, allows bots.
#   3. forexfactory.com — original, blocks most bots but worth a try.
#
# If every source fails, is_high_impact_news_now() falls back to a
# TIME-BASED BLACKOUT — blocking trades during the fixed windows when
# major USD data almost always drops (8:30, 10:00, 14:00 EST).
# This means the bot is ALWAYS protected, even without internet.

FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")   # free at finnhub.io

# Known high-impact USD release windows (UTC hour, minute).
# EST = UTC-5 in winter, UTC-4 in summer — we use UTC to avoid DST bugs.
# 8:30 EST = 13:30 UTC | 10:00 EST = 15:00 UTC | 14:00 EST = 19:00 UTC
_HIGH_IMPACT_UTC_WINDOWS = [
    (13, 30),   # 8:30 AM EST  — NFP, CPI, PPI, jobless claims, retail sales
    (15,  0),   # 10:00 AM EST — ISM, consumer confidence, existing home sales
    (19,  0),   # 2:00 PM EST  — FOMC rate decision
    (19, 30),   # 2:30 PM EST  — FOMC press conference
]
_NEWS_BLACKOUT_MINS = 20   # block ±20 min around each window


def _is_in_time_blackout() -> bool:
    """
    Pure time-based guard — no network required.
    Blocks trading during the ±20-minute window around each known
    high-impact USD release time (in UTC).
    Used as a fallback when all calendar sources are unreachable.
    """
    now_utc = datetime.now(timezone.utc)
    for h, m in _HIGH_IMPACT_UTC_WINDOWS:
        window_start = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_secs   = (now_utc - window_start).total_seconds()
        if -(_NEWS_BLACKOUT_MINS * 60) <= delta_secs <= (_NEWS_BLACKOUT_MINS * 60):
            logger.warning(
                f"🕐 Time blackout: near known high-impact window "
                f"{h:02d}:{m:02d} UTC (±{_NEWS_BLACKOUT_MINS} min) — skipping trade"
            )
            return True
    return False


class _NewsCache:
    TTL_SECONDS = 1800   # fetch at most once every 30 minutes

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self):
        self._events: list[dict] = []
        self._label:  str        = "Offline"
        self._fetched_at: float  = 0.0
        self._source_ok:  str    = ""   # which source last succeeded

    def _is_stale(self) -> bool:
        return (time.time() - self._fetched_at) > self.TTL_SECONDS

    # ── Source 1: Finnhub ─────────────────────────────────────
    def _try_finnhub(self) -> list[dict] | None:
        """
        Finnhub free economic calendar.
        Endpoint: /calendar/economic  (returns JSON)
        Sign up free at https://finnhub.io — takes ~30 seconds.
        Add FINNHUB_KEY=your_key to your .env file.
        """
        if not FINNHUB_KEY:
            return None   # skip silently if not configured
        today = datetime.now()
        week_end = today + __import__('datetime').timedelta(days=7)
        url = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={today.strftime('%Y-%m-%d')}"
            f"&to={week_end.strftime('%Y-%m-%d')}"
            f"&token={FINNHUB_KEY}"
        )
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        events = []
        for ev in data.get("economicCalendar", []):
            if ev.get("country") != "US":
                continue
            impact = ev.get("impact", "").capitalize()
            if impact not in ("High", "Medium"):
                continue
            event_dt = None
            try:
                event_dt = datetime.strptime(ev["time"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            events.append({
                "title":  ev.get("event", "Unknown"),
                "impact": impact,
                "dt":     event_dt,
            })
        return events

    # ── Source 2 & 3: ForexFactory XML (mirror + original) ────
    def _try_ff_xml(self, url: str) -> list[dict] | None:
        r = requests.get(url, headers=self.HEADERS, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        now  = datetime.now()
        events = []
        for item in root.findall("event"):
            impact_el   = item.find("impact")
            title_el    = item.find("title")
            currency_el = item.find("currency")
            date_el     = item.find("date")
            time_el     = item.find("time")
            if not impact_el or not title_el:
                continue
            impact   = impact_el.text   or ""
            currency = currency_el.text if currency_el is not None else ""
            if impact not in ("High", "Medium") or currency != "USD":
                continue
            event_dt = None
            raw_time = (time_el.text or "").strip() if time_el is not None else ""
            if date_el is not None and raw_time:
                try:
                    event_dt = datetime.strptime(
                        f"{date_el.text} {raw_time}", "%a %b %d %I:%M%p"
                    ).replace(year=now.year)
                except Exception:
                    pass
            events.append({
                "title":  title_el.text or "",
                "impact": impact,
                "dt":     event_dt,
            })
        return events

    def _fetch_and_parse(self):
        """Try all sources in order. Cache whichever succeeds first."""
        sources = [
            ("Finnhub",          lambda: self._try_finnhub()),
            ("nfs.faireconomy",  lambda: self._try_ff_xml(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.xml")),
            ("ForexFactory",     lambda: self._try_ff_xml(
                "https://www.forexfactory.com/ff_calendar_thisweek.xml")),
        ]

        for name, fetcher in sources:
            try:
                events = fetcher()
                if events is None:
                    continue   # source skipped (e.g. no API key)
                self._events     = events
                self._fetched_at = time.time()
                self._source_ok  = name
                tags = [
                    ("🔴" if e["impact"] == "High" else "🟡") + e["title"]
                    for e in events[:2]
                ]
                self._label = " | ".join(tags) if tags else "Quiet"
                logger.info(
                    f"📰 News cache refreshed via {name} "
                    f"({len(events)} USD events) — next fetch in 30 min"
                )
                return   # success
            except requests.exceptions.HTTPError as e:
                logger.warning(f"⚠️  News [{name}] HTTP {e.response.status_code} — trying next")
            except requests.exceptions.Timeout:
                logger.warning(f"⚠️  News [{name}] timed out — trying next")
            except Exception as e:
                logger.warning(f"⚠️  News [{name}] error: {e} — trying next")

        # All sources failed
        if self._fetched_at > 0:
            logger.warning("⚠️  All news sources down — reusing cached data")
        else:
            logger.error("❌ All news sources failed and no cache — using time blackout")
            self._label = "Offline"

    def get_label(self) -> str:
        if self._is_stale():
            self._fetch_and_parse()
        return self._label

    def is_high_impact_imminent(self, window_secs: int = 900) -> bool:
        """
        Returns True if a High-impact USD event is within window_secs.
        Falls back to time-based blackout if all sources are unavailable.
        """
        if self._is_stale():
            self._fetch_and_parse()

        # If we have real calendar data, use it
        if self._fetched_at > 0 and self._events:
            now = datetime.now()
            for ev in self._events:
                if ev["impact"] != "High" or ev["dt"] is None:
                    continue
                delta = (ev["dt"] - now).total_seconds()
                if -window_secs <= delta <= window_secs:
                    mins = round(delta / 60, 1)
                    logger.warning(
                        f"📰 High-impact news in {mins} min ({ev['title']}) — skipping trade"
                    )
                    return True
            return False

        # No calendar data at all — fall back to time-based blackout
        return _is_in_time_blackout()


_news_cache = _NewsCache()


def fetch_news() -> str:
    """Return a short string of upcoming high/medium USD news events."""
    return _news_cache.get_label()


def is_high_impact_news_now() -> bool:
    """True if a High-impact USD event is within 15 minutes of now."""
    return _news_cache.is_high_impact_imminent(window_secs=900)

def technical_confirmation(signal: str, df: pd.DataFrame, h1_trend: str) -> tuple[bool, str]:
    """
    Pure technical gate — replaces the LLM confirmation entirely.
    Zero latency, zero API calls, never rate-limits.

    Rules (ALL must pass):
      1. H1 trend alignment — trade WITH the higher timeframe
      2. RSI not in extreme zone — avoids chasing overbought/oversold exhaustion
      3. ATR momentum — candle body >= 30% of ATR (real move, not drift)
      4. EMA separation — fast/slow gap >= 0.05% of price (trend has conviction)
      5. No back-to-back signals — previous candle must NOT also have been a signal
         in the same direction (prevents re-entering a stale move)
      6. No high-impact news within 15 minutes
    """
    curr  = df.iloc[-1]
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    rsi       = curr['RSI']
    atr       = curr['ATR']
    ema_fast  = curr['EMA_fast']
    ema_slow  = curr['EMA_slow']
    close     = curr['close']
    body_size = abs(curr['close'] - curr['open'])

    reasons = []

    # 1. H1 trend alignment
    if signal == "BUY"  and h1_trend != "UP":
        return False, f"TECH:H1={h1_trend} (need UP)"
    if signal == "SELL" and h1_trend != "DOWN":
        return False, f"TECH:H1={h1_trend} (need DOWN)"

    # 2. RSI not extended (avoid chasing)
    if signal == "BUY"  and rsi > 65:
        return False, f"TECH:RSI={rsi:.0f} overbought"
    if signal == "SELL" and rsi < 30:
        return False, f"TECH:RSI={rsi:.0f} oversold"

    # 3. ATR momentum — candle body is a real move
    if atr > 0 and body_size < atr * 0.30:
        return False, f"TECH:body={body_size:.2f} < 30% ATR={atr:.2f} (drift)"

    # 4. EMA separation — trend has conviction, not a flat cross
    ema_sep_pct = abs(ema_fast - ema_slow) / close * 100
    if ema_sep_pct < 0.05:
        return False, f"TECH:EMA gap={ema_sep_pct:.3f}% (flat, no conviction)"

    # 5. No stale re-entry — previous candle must not already have been in signal direction
    prev_ema_bull  = prev['EMA_fast']  > prev['EMA_slow']
    prev2_ema_bull = prev2['EMA_fast'] > prev2['EMA_slow']
    if signal == "BUY"  and prev_ema_bull  and prev2_ema_bull:
        return False, "TECH:stale BUY (EMA cross already 2+ candles old)"
    if signal == "SELL" and not prev_ema_bull and not prev2_ema_bull:
        return False, "TECH:stale SELL (EMA cross already 2+ candles old)"

    # 6. News blackout
    if is_high_impact_news_now():
        return False, "TECH:high-impact news within 15 min"

    reason = (
        f"TECH:OK RSI={rsi:.0f} body={body_size:.1f} "
        f"ATR={atr:.1f} EMAgap={ema_sep_pct:.2f}% H1={h1_trend}"
    )
    return True, reason

# ─── TRADE MANAGEMENT ────────────────────────────────────────

def trail_stops():
    """Milestone Trail: Locks in profit in steps of 1,000."""
    STEP = 1000
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions:
        return

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
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl":       target_sl,
                    "tp":       pos.tp,
                })
        elif pos.type == mt5.POSITION_TYPE_SELL:
            target_sl = pos.price_open - price_offset
            if pos.sl == 0 or target_sl < pos.sl - (10 * symbol_info.point):
                mt5.order_send({
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl":       target_sl,
                    "tp":       pos.tp,
                })

def close_on_profit_target(target: float = 1000.0):
    """Close any position that hits the profit target."""
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions:
        return
    for pos in positions:
        if pos.profit < target:
            continue
        tick        = mt5.symbol_info_tick(SYMBOL)
        type_close  = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price_close = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
        res = mt5.order_send({
            "action":        mt5.TRADE_ACTION_DEAL,
            "position":      pos.ticket,
            "symbol":        SYMBOL,
            "volume":        pos.volume,
            "type":          type_close,
            "price":         price_close,
            "magic":         MAGIC_NUMBER,
            "type_time":     mt5.ORDER_TIME_GTC,
            "type_filling":  mt5.ORDER_FILLING_IOC,
        })
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"💰 TARGET: Closed #{pos.ticket} at profit {pos.profit:.2f}")
        else:
            logger.error(f"❌ Target close failed: {res.comment}")

# ─── MAIN ENGINE ─────────────────────────────────────────────

def run_bot():
    global _last_trade_time

    if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
        logger.error("❌ MT5 Init Failed")
        return

    logger.info("=" * 64)
    logger.info("🔥 SCURO ACCUMULATOR — REBUILT")
    logger.info(f"   Symbol: {SYMBOL} | Magic: {MAGIC_NUMBER}")
    logger.info(f"   Daily loss limit: {DAILY_LOSS_LIMIT:+.2f}")
    logger.info(f"   Max consecutive losses: {MAX_CONSECUTIVE_LOSSES}")
    logger.info("=" * 64)

    # Initial sync and config load
    logger.info("📥 Initial MT5 history sync...")
    sync_mt5_history()
    load_adaptive_config()
    print_summary_table()

    last_min      = -1
    last_summary  = -1
    circuit_break_until = 0.0   # timestamp until circuit breaker expires

    while True:
        now   = datetime.now()
        rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, 100)
        if rates is None:
            time.sleep(1)
            continue

        df = pd.DataFrame(rates)
        # Use adaptive EMA periods
        ema_fast_period = int(_ADAPTIVE['ema_fast'])
        ema_slow_period = int(_ADAPTIVE['ema_slow'])
        df['EMA_fast'] = ta.ema(df['close'], ema_fast_period)
        df['EMA_slow'] = ta.ema(df['close'], ema_slow_period)
        df['RSI']      = ta.rsi(df['close'], 14)
        df['ATR']      = ta.atr(df['high'], df['low'], df['close'], 14)

        curr = df.iloc[-1]

        # Always manage open positions (profit targets + trailing stops)
        close_on_profit_target(1000.0)
        trail_stops()

        # Once-per-minute tasks
        if now.minute != last_min:
            # Hot-reload config from advisor
            load_adaptive_config()

            # Sync MT5 history to CSV (diagnostic log)
            sync_mt5_history()

            # Summary print
            summary_slot = (now.hour * 60 + now.minute) // SUMMARY_INTERVAL
            if summary_slot != last_summary:
                print_summary_table()
                last_summary = summary_slot

            # ── Circuit breaker checks ────────────────────────
            trading_halted = False

            if time.time() < circuit_break_until:
                remaining = (circuit_break_until - time.time()) / 60
                logger.info(f"⏸️  Circuit breaker active — {remaining:.0f} min remaining")
                trading_halted = True

            if not trading_halted and _ADAPTIVE.get('circuit_breaker_daily_loss_enabled', True) and check_daily_loss_circuit_breaker():
                # Daily loss — halt until end of day
                end_of_day = now.replace(hour=23, minute=59, second=59)
                circuit_break_until = end_of_day.timestamp()
                trading_halted = True

            if not trading_halted and _ADAPTIVE.get('circuit_breaker_consecutive_losses_enabled', True) and check_consecutive_loss_circuit_breaker():
                # Consecutive losses — pause for cooldown_mins
                pause_secs = _ADAPTIVE['cooldown_mins'] * 60
                circuit_break_until = time.time() + pause_secs
                trading_halted = True

            if not trading_halted:
                # ── Cooldown check ────────────────────────────
                mins_since_last = (time.time() - _last_trade_time) / 60
                if _last_trade_time > 0 and mins_since_last < _ADAPTIVE['cooldown_mins']:
                    remaining_cool = _ADAPTIVE['cooldown_mins'] - mins_since_last
                    logger.info(f"⏱️  Cooldown: {remaining_cool:.0f} min remaining")
                    trading_halted = True

            if not trading_halted:
                # ── Open position cap check ───────────────────
                open_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
                open_count = len(open_pos) if open_pos else 0
                if open_count >= _ADAPTIVE['max_open_positions']:
                    logger.info(f"📊 Max positions reached ({open_count}/{int(_ADAPTIVE['max_open_positions'])}) — skipping signal")
                    trading_halted = True

            if not trading_halted:
                # ── Signal generation ─────────────────────────
                h1_trend = get_h1_context()
                rsi_val  = curr['RSI']
                rsi_buy  = _ADAPTIVE['rsi_buy_threshold']
                rsi_sell = _ADAPTIVE['rsi_sell_threshold']

                logger.info(
                    f"🔍 {SYMBOL} | Price: {curr['close']:.2f} | RSI: {rsi_val:.1f} | "
                    f"H1: {h1_trend} | EMA: {'↑' if curr['EMA_fast'] > curr['EMA_slow'] else '↓'} | "
                    f"Streak: {count_consecutive_losses_current()}L"
                )

                signal = "NONE"
                ema_bull = curr['EMA_fast'] > curr['EMA_slow']
                ema_bear = curr['EMA_fast'] < curr['EMA_slow']

                # Only trade WITH the H1 trend to avoid fighting the trend
                if ema_bull and rsi_val > rsi_buy and h1_trend == "UP":
                    signal = "BUY"
                elif ema_bear and rsi_val < rsi_sell and h1_trend == "DOWN":
                    signal = "SELL"
                else:
                    # Explain exactly why no signal — speeds up debugging
                    no_sig_reasons = []
                    if not ema_bull and not ema_bear:
                        no_sig_reasons.append("EMA flat")
                    elif ema_bull and h1_trend != "UP":
                        no_sig_reasons.append(f"EMA↑ but H1={h1_trend} (need UP)")
                    elif ema_bear and h1_trend != "DOWN":
                        no_sig_reasons.append(f"EMA↓ but H1={h1_trend} (need DOWN)")
                    if ema_bull and rsi_val <= rsi_buy:
                        no_sig_reasons.append(f"RSI={rsi_val:.1f} <= buy threshold {rsi_buy}")
                    if ema_bear and rsi_val >= rsi_sell:
                        no_sig_reasons.append(f"RSI={rsi_val:.1f} >= sell threshold {rsi_sell}")
                    logger.info(f"   ↳ No signal: {' | '.join(no_sig_reasons) if no_sig_reasons else 'conditions not met'}")

                if signal != "NONE":
                    news = fetch_news()
                    logger.info(f"📡 {signal} signal detected | Running technical gate...")

                    ok, reason = technical_confirmation(signal, df, h1_trend)

                    if ok:
                        # AutoTrading guard
                        term_info = mt5.terminal_info()
                        if term_info and not term_info.trade_allowed:
                            logger.error(
                                "❌ AutoTrading DISABLED — enable in MT5 toolbar "
                                "(Tools → Options → Expert Advisors)"
                            )
                            last_min = now.minute
                            time.sleep(1)
                            continue

                        tick    = mt5.symbol_info_tick(SYMBOL)
                        price   = tick.ask if signal == "BUY" else tick.bid
                        sl_dist = curr['ATR'] * _ADAPTIVE['sl_atr_mult']
                        sl      = price - sl_dist if signal == "BUY" else price + sl_dist
                        tp      = (price + sl_dist * _ADAPTIVE['reward_ratio']
                                   if signal == "BUY"
                                   else price - sl_dist * _ADAPTIVE['reward_ratio'])

                        res = mt5.order_send({
                            "action":        mt5.TRADE_ACTION_DEAL,
                            "symbol":        SYMBOL,
                            "volume":        float(_ADAPTIVE["accumulation_lot"]),
                            "type":          mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL,
                            "price":         price,
                            "sl":            sl,
                            "tp":            tp,
                            "magic":         MAGIC_NUMBER,
                            "type_time":     mt5.ORDER_TIME_GTC,
                            "type_filling":  mt5.ORDER_FILLING_IOC,
                        })

                        if res.retcode == mt5.TRADE_RETCODE_DONE:
                            _last_trade_time = time.time()
                            logger.info(
                                f"✅ TRADE PLACED: {signal} @ {price:.2f} | "
                                f"SL: {sl:.2f} | TP: {tp:.2f} | AI: {reason}"
                            )
                            record_trade_open(
                                ticket=res.order, signal=signal,
                                price=price, sl=sl, tp=tp,
                                rsi=rsi_val, h1_trend=h1_trend,
                                news=news, ai_reason=reason,
                            )
                        else:
                            logger.error(f"❌ Execution error: {res.comment}")
                    else:
                        logger.info(f"⏭️  Technical gate rejected: {reason}")

            last_min = now.minute

        time.sleep(1)

if __name__ == "__main__":
    run_bot()
