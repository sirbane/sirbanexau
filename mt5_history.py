"""
mt5_history.py  ─  Scuro MT5 Live History Printer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pulls live data from MT5 terminal and writes it to
scuro_live_data.json — exactly what the MT5 History
tab shows. The Streamlit dashboard reads this file.

Run in a separate terminal (alongside xau.py + advisor.py):
    python mt5_history.py

Output file: scuro_live_data.json (refreshed every 10s)
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

import MetaTrader5 as mt5
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    handlers=[
        logging.FileHandler("mt5_history.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()
MT5_LOGIN    = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER   = os.getenv("MT5_SERVER")

SYMBOL        = "XAUUSDm"
MAGIC_NUMBER  = 777777
OUTPUT_JSON   = "scuro_live_data.json"
REFRESH_SECS  = 10           # how often to refresh
LOOKBACK_DAYS = 30           # history window


def fmt_ts(unix_ts) -> str:
    return datetime.fromtimestamp(int(unix_ts)).strftime("%Y-%m-%d %H:%M:%S")


def pull_all_data() -> dict:
    """Pull everything from MT5 and return as a serialisable dict."""

    date_from = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    date_to   = datetime.now() + timedelta(days=1)

    # ── 1. Closed deals (History tab) ─────────────────────────────
    deals = mt5.history_deals_get(date_from, date_to) or []

    entry_deals: dict = {}   # position_id → opening deal
    exit_deals:  dict = {}   # position_id → closing deal

    for d in deals:
        if d.symbol != SYMBOL:
            continue
        pid = str(d.position_id)
        if d.entry == mt5.DEAL_ENTRY_IN:
            entry_deals[pid] = d
        elif d.entry == mt5.DEAL_ENTRY_OUT:
            exit_deals[pid] = d

    # ── 2. Build closed-trade rows ─────────────────────────────────
    closed_trades = []
    for pid, out in exit_deals.items():
        inn = entry_deals.get(pid)

        # Fetch SL/TP from historical orders
        sl, tp = None, None
        orders = mt5.history_orders_get(position=int(pid)) or []
        for o in orders:
            if o.sl and not sl: sl = round(o.sl, 2)
            if o.tp and not tp: tp = round(o.tp, 2)

        profit    = round(out.profit, 2)
        direction = "BUY" if (inn and inn.type == mt5.DEAL_TYPE_BUY) else "SELL"
        outcome   = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BE")

        closed_trades.append({
            "ticket":      pid,
            "magic":       int(out.magic),
            "open_time":   fmt_ts(inn.time) if inn else "",
            "close_time":  fmt_ts(out.time),
            "open_ts":     int(inn.time) if inn else int(out.time),
            "close_ts":    int(out.time),
            "symbol":      SYMBOL,
            "direction":   direction,
            "volume":      round(inn.volume if inn else out.volume, 2),
            "entry_price": round(inn.price if inn else out.price, 2),
            "close_price": round(out.price, 2),
            "sl":          sl,
            "tp":          tp,
            "profit":      profit,
            "swap":        round(out.swap, 2),
            "commission":  round(out.commission, 2),
            "outcome":     outcome,
            "comment":     out.comment or "",
        })

    # Sort newest first
    closed_trades.sort(key=lambda x: x["close_ts"], reverse=True)

    # ── 3. Open positions ──────────────────────────────────────────
    open_positions = []
    pos_list = mt5.positions_get(symbol=SYMBOL) or []
    tick = mt5.symbol_info_tick(SYMBOL)

    for p in pos_list:
        direction   = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        current_px  = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
        float_pnl   = round(p.profit, 2)
        pips        = (current_px - p.price_open) if direction == "BUY" else (p.price_open - current_px)
        hold_mins   = (time.time() - p.time) / 60

        open_positions.append({
            "ticket":      str(p.ticket),
            "magic":       int(p.magic),
            "open_time":   fmt_ts(p.time),
            "open_ts":     int(p.time),
            "symbol":      p.symbol,
            "direction":   direction,
            "volume":      round(p.volume, 2),
            "entry_price": round(p.price_open, 2),
            "current_px":  round(current_px, 2),
            "sl":          round(p.sl, 2) if p.sl else None,
            "tp":          round(p.tp, 2) if p.tp else None,
            "float_pnl":   float_pnl,
            "swap":        round(p.swap, 2),
            "pips":        round(pips, 2),
            "hold_mins":   round(hold_mins, 1),
            "comment":     p.comment or "",
        })

    # ── 4. Account stats ───────────────────────────────────────────
    acct = mt5.account_info()
    account = {}
    if acct:
        account = {
            "login":       acct.login,
            "name":        acct.name,
            "server":      acct.server,
            "broker":      acct.company,
            "currency":    acct.currency,
            "balance":     round(acct.balance, 2),
            "equity":      round(acct.equity, 2),
            "margin":      round(acct.margin, 2),
            "free_margin": round(acct.margin_free, 2),
            "margin_pct":  round(acct.margin_level, 2) if acct.margin_level else 0,
            "drawdown_pct": round((1 - acct.equity / acct.balance) * 100, 2) if acct.balance > 0 else 0,
            "leverage":    acct.leverage,
            "type":        "demo" if acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "live",
        }

    # ── 5. Performance stats (robot trades only) ───────────────────
    robot_closed = [t for t in closed_trades if t["magic"] == MAGIC_NUMBER]
    all_closed   = closed_trades  # all symbols+magics

    def _stats(trades: list, label: str) -> dict:
        if not trades:
            return {f"{label}_trades": 0}
        profits = [t["profit"] for t in trades]
        wins    = [p for p in profits if p > 0]
        losses  = [p for p in profits if p < 0]
        gp      = sum(wins)
        gl      = abs(sum(losses))
        pf      = round(gp / gl, 3) if gl > 0 else 999.0
        wr      = round(len(wins) / len(profits) * 100, 1)

        # Consecutive loss streak
        outcomes = ["WIN" if p > 0 else "LOSS" for p in profits]  # newest first
        streak, s_type = 0, outcomes[0] if outcomes else ""
        for o in outcomes:
            if o == s_type: streak += 1
            else: break

        # Max consecutive losses
        max_cons = cur = 0
        for o in reversed(outcomes):  # oldest→newest
            if o == "LOSS": cur += 1; max_cons = max(max_cons, cur)
            else: cur = 0

        return {
            f"{label}_trades":                len(trades),
            f"{label}_wins":                  len(wins),
            f"{label}_losses":                len(losses),
            f"{label}_win_rate_pct":          wr,
            f"{label}_net_pnl":               round(sum(profits), 2),
            f"{label}_gross_profit":          round(gp, 2),
            f"{label}_gross_loss":            round(gl, 2),
            f"{label}_profit_factor":         pf,
            f"{label}_avg_win":               round(gp / len(wins), 2)     if wins   else 0,
            f"{label}_avg_loss":              round(gl / len(losses), 2)   if losses else 0,
            f"{label}_best_trade":            round(max(profits), 2),
            f"{label}_worst_trade":           round(min(profits), 2),
            f"{label}_current_streak":        streak,
            f"{label}_current_streak_type":   s_type,
            f"{label}_max_consecutive_losses": max_cons,
            f"{label}_last_5_outcomes":       outcomes[:5],
        }

    stats = {}
    stats.update(_stats(robot_closed, "robot"))
    stats.update(_stats(all_closed,   "total"))

    # ── 6. Equity curve (from closed trades, newest first) ─────────
    equity_curve = []
    running = account.get("balance", 0)
    # Walk from oldest to newest, reconstructing balance history
    for t in reversed(closed_trades):
        equity_curve.append({
            "ts":      t["close_ts"],
            "time":    t["close_time"],
            "balance": round(running, 2),
        })
        running -= t["profit"]  # going back in time

    equity_curve.reverse()

    # ── 7. Today's P&L ────────────────────────────────────────────
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_trades = [
        t for t in robot_closed
        if t["close_ts"] >= today_start.timestamp()
    ]
    today_pnl  = round(sum(t["profit"] for t in today_trades), 2)
    today_wins  = sum(1 for t in today_trades if t["profit"] > 0)
    today_loss  = sum(1 for t in today_trades if t["profit"] < 0)

    # ── 8. Load advisor config ─────────────────────────────────────
    advisor_config = {}
    if os.path.exists("scuro_config.json"):
        try:
            with open("scuro_config.json") as f:
                advisor_config = json.load(f)
        except Exception:
            pass

    # ── 9. Live tick ───────────────────────────────────────────────
    live_tick = {}
    if tick:
        live_tick = {
            "bid":   round(tick.bid, 2),
            "ask":   round(tick.ask, 2),
            "spread": round(tick.ask - tick.bid, 2),
            "time":  fmt_ts(tick.time),
        }

    # ── 10. Current M5 RSI/ATR for live signals ───────────────────
    live_indicators = {}
    try:
        import pandas_ta as ta
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 50)
        if rates is not None:
            df = pd.DataFrame(rates)
            ema_f = int(advisor_config.get("ema_fast", 5))
            ema_s = int(advisor_config.get("ema_slow", 13))
            df["RSI"]      = ta.rsi(df["close"], 14)
            df["ATR"]      = ta.atr(df["high"], df["low"], df["close"], 14)
            df["EMA_fast"] = ta.ema(df["close"], ema_f)
            df["EMA_slow"] = ta.ema(df["close"], ema_s)
            curr = df.iloc[-1]
            live_indicators = {
                "rsi":          round(float(curr["RSI"]), 1),
                "atr":          round(float(curr["ATR"]), 2),
                "ema_fast":     round(float(curr["EMA_fast"]), 2),
                "ema_slow":     round(float(curr["EMA_slow"]), 2),
                "ema_trend":    "UP" if curr["EMA_fast"] > curr["EMA_slow"] else "DOWN",
                "close":        round(float(curr["close"]), 2),
            }
    except Exception as e:
        live_indicators = {"error": str(e)}

    # ── 11. H1 trend ──────────────────────────────────────────────
    h1_trend = "UNKNOWN"
    try:
        import pandas_ta as ta
        h1_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
        if h1_rates is not None:
            h1df  = pd.DataFrame(h1_rates)
            ema50 = ta.ema(h1df["close"], 50).iloc[-1]
            h1_trend = "UP" if h1df["close"].iloc[-1] > ema50 else "DOWN"
    except Exception:
        pass

    return {
        "meta": {
            "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":       SYMBOL,
            "magic":        MAGIC_NUMBER,
            "lookback_days": LOOKBACK_DAYS,
        },
        "account":         account,
        "live_tick":       live_tick,
        "live_indicators": live_indicators,
        "h1_trend":        h1_trend,
        "stats":           stats,
        "today": {
            "pnl":    today_pnl,
            "trades": len(today_trades),
            "wins":   today_wins,
            "losses": today_loss,
        },
        "open_positions":  open_positions,
        "closed_trades":   closed_trades[:500],   # cap at 500 for JSON size
        "equity_curve":    equity_curve,
        "advisor_config":  advisor_config,
    }


def run():
    if not mt5.initialize(
        login=int(MT5_LOGIN),
        password=MT5_PASSWORD,
        server=MT5_SERVER,
    ):
        logger.error("❌ MT5 Init Failed")
        return

    logger.info("=" * 60)
    logger.info("📡 SCURO MT5 HISTORY PRINTER — Live Feed")
    logger.info(f"   Symbol   : {SYMBOL} | Magic: {MAGIC_NUMBER}")
    logger.info(f"   Output   : {os.path.abspath(OUTPUT_JSON)}")
    logger.info(f"   Refresh  : every {REFRESH_SECS}s")
    logger.info("=" * 60)

    while True:
        try:
            # Reconnect if needed
            if not mt5.terminal_info():
                logger.warning("⚠️  MT5 disconnected — reconnecting...")
                if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
                    logger.error("❌ Reconnect failed — retrying in 30s")
                    time.sleep(30)
                    continue

            data = pull_all_data()

            with open(OUTPUT_JSON, "w") as f:
                json.dump(data, f, indent=2)

            n_open   = len(data["open_positions"])
            n_closed = data["stats"].get("robot_trades", 0)
            pf       = data["stats"].get("robot_profit_factor", 0)
            wr       = data["stats"].get("robot_win_rate_pct", 0)
            bal      = data["account"].get("balance", 0)

            logger.info(
                f"✅ Updated {OUTPUT_JSON} | "
                f"Open: {n_open} | Closed: {n_closed} | "
                f"WR: {wr}% | PF: {pf} | Bal: {bal:,.2f}"
            )

        except Exception as e:
            logger.error(f"❌ Error: {e}")

        time.sleep(REFRESH_SECS)


if __name__ == "__main__":
    run()
