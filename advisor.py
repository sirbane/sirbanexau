"""
advisor.py  ─  Scuro Adaptive Learning Engine (MT5-Native Edition)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs alongside xau.py. Every ADVISOR_INTERVAL minutes it:
  1. Pulls REAL trade history directly from MT5 via history_deals_get()
  2. Computes authoritative performance stats (no CSV approximations)
  3. Sends stats + current config to Llama 4 Scout for parameter advice
  4. Parses the model's JSON parameter recommendations
  5. Validates against hard safety bounds
  6. Writes validated changes to scuro_config.json
  7. xau.py hot-reloads that file each minute — no restart needed

KEY IMPROVEMENT over previous version:
  Stats come from MT5 directly — not from trade_history.csv.
  This means ALL trades (manual, robot, any session) are counted accurately.
  The advisor sees exactly what the MT5 History tab shows.

Run in a separate terminal:
    python advisor.py
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

import MetaTrader5 as mt5
import pandas as pd
import requests

# ─── LOGGING ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    handlers=[
        logging.FileHandler("advisor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MT5_LOGIN    = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER   = os.getenv("MT5_SERVER")

# ─── PATHS ───────────────────────────────────────────────────
CONFIG_JSON   = "scuro_config.json"
CHANGELOG_LOG = "advisor_changelog.log"

# ─── ADVISOR SETTINGS ────────────────────────────────────────
ADVISOR_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"
ADVISOR_INTERVAL = 30          # minutes between analysis cycles
MIN_CLOSED_TRADES = 3          # don't advise until we have enough data

# How far back to look for history (days). 30 gives enough context
# without drowning the model in ancient trades.
HISTORY_LOOKBACK_DAYS = 30

# Symbol and magic number must match xau.py exactly
SYMBOL       = "XAUUSDm"
MAGIC_NUMBER = 777777

# ─── PARAMETER BOUNDS (hard safety limits) ───────────────────
# Advisor CANNOT push values outside these ranges — ever.
PARAM_BOUNDS = {
    "accumulation_lot":   (0.01,  0.05),   # tightened — 0.10 was too aggressive
    "reward_ratio":       (1.5,   4.0),
    "rsi_buy_threshold":  (42,    52),      # wider floor so bot can be more selective
    "rsi_sell_threshold": (48,    58),
    "sl_atr_mult":        (1.0,   3.0),    # allow wider SL to avoid stops getting hunted
    "trail_atr_mult":     (0.8,   2.5),
    "ema_fast":           (3,     10),
    "ema_slow":           (7,     21),
    "max_open_positions": (1,     3),       # NEW: advisor can tighten concurrent trades
    "cooldown_mins":      (5,     60),      # NEW: advisor can enforce longer cooldowns
}

# ─── DEFAULT CONFIG ──────────────────────────────────────────
DEFAULT_CONFIG = {
    "accumulation_lot":   0.02,
    "reward_ratio":       2.5,     # raised from 2.0 — need bigger wins to offset losses
    "rsi_buy_threshold":  45,      # raised from 47 — be more selective on buys
    "rsi_sell_threshold": 55,      # lowered from 53 — be more selective on sells
    "sl_atr_mult":        2.2,     # widened from 1.8 — prevent stop-hunts on gold
    "trail_atr_mult":     1.4,
    "ema_fast":           5,
    "ema_slow":           13,      # widened from 8 — reduces noise-triggered signals
    "max_open_positions": 2,       # start conservative
    "cooldown_mins":      15,      # start with 15-min cooldown between trades
    "last_updated":       "",
    "update_reason":      "default",
    "cycle":              0,
}

# ─── CONFIG HELPERS ──────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON) as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            logger.warning(f"⚠️  Config read error: {e} — using defaults")
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    with open(CONFIG_JSON, "w") as f:
        json.dump(cfg, f, indent=2)

def init_config():
    if not os.path.exists(CONFIG_JSON):
        save_config(DEFAULT_CONFIG.copy())
        logger.info(f"📁 Created default {CONFIG_JSON}")

# ─── MT5 HISTORY STATS ───────────────────────────────────────

def pull_mt5_stats() -> dict | None:
    """
    Pull closed trade stats DIRECTLY from MT5 history_deals_get().
    This is the exact same source as the MT5 'History' tab — 100% accurate.

    Returns a stats dict, or None if MT5 is unavailable / no data.
    """
    date_from = datetime.now() - timedelta(days=HISTORY_LOOKBACK_DAYS)
    date_to   = datetime.now() + timedelta(days=1)

    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None or len(deals) == 0:
        logger.warning("⚠️  MT5 returned no deal history.")
        return None

    df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())

    # Filter: only closed (exit) deals for our symbol
    # entry == DEAL_ENTRY_OUT (1) = the closing leg of a trade
    closed = df[
        (df["entry"] == mt5.DEAL_ENTRY_OUT) &
        (df["symbol"] == SYMBOL)
    ].copy()

    if closed.empty:
        logger.info("📋 No closed deals found in MT5 history.")
        return None

    # Separate robot trades vs manual trades
    robot_closed  = closed[closed["magic"] == MAGIC_NUMBER]
    all_closed    = closed   # includes manual + robot

    def _calc(trades: pd.DataFrame, label: str) -> dict:
        if trades.empty:
            return {}
        profits = trades["profit"].astype(float)
        wins    = trades[profits > 0]
        losses  = trades[profits < 0]

        gross_profit = wins["profit"].sum()
        gross_loss   = abs(losses["profit"].sum())
        net_pnl      = gross_profit - gross_loss
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0
        win_rate      = round(len(wins) / len(trades) * 100, 1) if len(trades) > 0 else 0.0

        # Consecutive loss streak (look at most recent trades)
        sorted_trades  = trades.sort_values("time")
        outcomes_list  = ["WIN" if p > 0 else "LOSS" for p in sorted_trades["profit"]]
        current_streak, streak_type = 0, ""
        if outcomes_list:
            streak_type = outcomes_list[-1]
            for o in reversed(outcomes_list):
                if o == streak_type: current_streak += 1
                else: break

        # Max consecutive losses
        max_cons_loss = 0
        cur = 0
        for o in outcomes_list:
            if o == "LOSS":
                cur += 1
                max_cons_loss = max(max_cons_loss, cur)
            else:
                cur = 0

        # Average hold time (in minutes)
        avg_hold_mins = None
        if "time" in trades.columns and "time_msc" not in trades.columns:
            # MT5 deals have `time` (open) but closing deals also carry position entry time
            # Use the deal time range as a proxy
            pass

        return {
            f"{label}_trades":              len(trades),
            f"{label}_wins":                len(wins),
            f"{label}_losses":              len(losses),
            f"{label}_win_rate_pct":        win_rate,
            f"{label}_net_pnl":             round(net_pnl, 2),
            f"{label}_gross_profit":        round(gross_profit, 2),
            f"{label}_gross_loss":          round(gross_loss, 2),
            f"{label}_profit_factor":       profit_factor,
            f"{label}_avg_win":             round(wins["profit"].mean(), 2)   if len(wins)   > 0 else 0,
            f"{label}_avg_loss":            round(losses["profit"].mean(), 2) if len(losses) > 0 else 0,
            f"{label}_best_trade":          round(profits.max(), 2),
            f"{label}_worst_trade":         round(profits.min(), 2),
            f"{label}_current_streak":      current_streak,
            f"{label}_current_streak_type": streak_type,
            f"{label}_max_consecutive_losses": max_cons_loss,
            f"{label}_last_5_outcomes":     outcomes_list[-5:],
        }

    stats = {}
    stats.update(_calc(robot_closed, "robot"))    # bot-only trades
    stats.update(_calc(all_closed,   "total"))    # all trades including manual

    # Add live account info
    acct = mt5.account_info()
    if acct:
        stats["account_balance"]  = round(acct.balance, 2)
        stats["account_equity"]   = round(acct.equity, 2)
        stats["account_drawdown_pct"] = round((1 - acct.equity / acct.balance) * 100, 2) if acct.balance > 0 else 0

    # Add current open positions count
    open_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    stats["open_positions"] = len(open_pos) if open_pos else 0

    logger.info(
        f"📊 MT5 Stats (Robot) → Trades: {stats.get('robot_trades', 0)} | "
        f"WR: {stats.get('robot_win_rate_pct', 0)}% | "
        f"PF: {stats.get('robot_profit_factor', 0)} | "
        f"PnL: {stats.get('robot_net_pnl', 0):+.2f} | "
        f"Streak: {stats.get('robot_current_streak', 0)}x{stats.get('robot_current_streak_type', '')} | "
        f"Balance: {stats.get('account_balance', 0):,.2f}"
    )

    return stats

# ─── LLAMA ADVISOR ───────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert algorithmic trading advisor for a XAUUSD scalping bot (XAUUSDm).
You receive LIVE performance statistics pulled directly from the MT5 terminal history.
Your job: recommend SPECIFIC parameter changes that will improve profitability and reduce drawdown.

Critical context about this bot's recent failures:
- Profit Factor below 1.0 means the bot is losing money overall
- Max drawdown above 30% is dangerous — prioritize capital preservation
- Over 100 trades per day = overtrading — use cooldown_mins and RSI thresholds to filter
- Consecutive losses > 5 = the strategy is in a losing regime — tighten filters aggressively

You MUST respond with ONLY a valid JSON object — no markdown, no explanation outside the JSON.
The JSON must have exactly this structure:
{
  "changes": {
    "param_name": new_value,
    ...
  },
  "reasoning": "one concise sentence explaining the key insight",
  "confidence": "HIGH | MEDIUM | LOW"
}

Only include parameters you actually want to change in "changes".
If the strategy is performing well (PF > 1.3, WR > 55%, drawdown < 20%), return: {"changes": {}, ...}

Tunable parameters and their types:
  accumulation_lot    (float, e.g. 0.02)   — position size; reduce if losing
  reward_ratio        (float, e.g. 2.5)    — TP/SL ratio; increase to improve PF
  rsi_buy_threshold   (int,   e.g. 45)     — raise to be more selective on buys
  rsi_sell_threshold  (int,   e.g. 55)     — lower to be more selective on sells
  sl_atr_mult         (float, e.g. 1.8)    — SL width in ATR multiples; increase if stops being hunted
  trail_atr_mult      (float, e.g. 1.4)    — trailing stop distance
  ema_fast            (int,   e.g. 5)      — fast EMA period
  ema_slow            (int,   e.g. 13)     — slow EMA period; must be > ema_fast
  max_open_positions  (int,   e.g. 2)      — max concurrent trades; reduce if overexposed
  cooldown_mins       (int,   e.g. 15)     — minutes between trades; increase to reduce overtrading
"""

def ask_llama(stats: dict, config: dict) -> dict | None:
    prompt = f"""
LIVE MT5 PERFORMANCE STATISTICS (pulled from terminal history tab):
{json.dumps(stats, indent=2)}

CURRENT BOT PARAMETERS:
{json.dumps({k: config[k] for k in PARAM_BOUNDS if k in config}, indent=2)}

Analysis cycle: #{config.get('cycle', 0) + 1}
Previous update reason: {config.get('update_reason', 'N/A')}

Key concerns to address:
- If robot_profit_factor < 1.0: the bot is losing — tighten filters immediately
- If robot_max_consecutive_losses > 5: pause by raising cooldown_mins to 45+
- If robot_trades > 50 per day equivalent: reduce overtrading via cooldown_mins / RSI thresholds
- If account_drawdown_pct > 30: reduce accumulation_lot and max_open_positions

Recommend SPECIFIC parameter adjustments. Respond ONLY with the JSON object.
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":    ADVISOR_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens":  512,
        "temperature": 0.2,   # very low — we want consistent, analytical decisions
    }

    raw_text = ""
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=30
        )
        data = resp.json()

        if "choices" not in data:
            err = data.get("error", {})
            logger.error(f"❌ Groq error [{err.get('type')}]: {err.get('message')}")
            return None

        raw_text = data["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if model added them
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        return json.loads(raw_text)

    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON parse error: {e}\nRaw: {raw_text[:300]}")
        return None
    except requests.exceptions.Timeout:
        logger.error("❌ Llama request timed out")
        return None
    except Exception as e:
        logger.error(f"❌ Advisor request failed: {e}")
        return None

# ─── VALIDATION & APPLICATION ────────────────────────────────

def validate_and_apply(recommendation: dict, current_config: dict):
    changes    = recommendation.get("changes", {})
    reasoning  = recommendation.get("reasoning", "no reason given")
    confidence = recommendation.get("confidence", "UNKNOWN")
    applied    = {}
    rejected   = {}

    new_config = current_config.copy()

    for param, new_val in changes.items():
        if param not in PARAM_BOUNDS:
            rejected[param] = f"unknown parameter"
            continue

        lo, hi = PARAM_BOUNDS[param]
        try:
            if isinstance(lo, int):
                new_val = int(new_val)
            else:
                new_val = float(round(float(new_val), 3))
        except (ValueError, TypeError):
            rejected[param] = f"bad type: {new_val}"
            continue

        if not (lo <= new_val <= hi):
            rejected[param] = f"{new_val} out of bounds [{lo}, {hi}]"
            continue

        old_val = current_config.get(param)
        if new_val == old_val:
            continue

        new_config[param] = new_val
        applied[param] = {"from": old_val, "to": new_val}

    # Sanity: ema_fast < ema_slow
    if new_config.get("ema_fast", 5) >= new_config.get("ema_slow", 13):
        rejected["ema_cross"] = "ema_fast must be < ema_slow — reverting both"
        new_config["ema_fast"] = current_config.get("ema_fast", 5)
        new_config["ema_slow"] = current_config.get("ema_slow", 13)

    # Sanity: rsi_buy < rsi_sell (gap must be at least 5 to avoid whipsaw)
    if new_config.get("rsi_buy_threshold", 45) >= new_config.get("rsi_sell_threshold", 55) - 4:
        rejected["rsi_gap"] = "rsi thresholds too close (need >= 5 gap) — reverting"
        new_config["rsi_buy_threshold"]  = current_config.get("rsi_buy_threshold", 45)
        new_config["rsi_sell_threshold"] = current_config.get("rsi_sell_threshold", 55)

    new_config["last_updated"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_config["update_reason"] = reasoning
    new_config["cycle"]         = current_config.get("cycle", 0) + 1

    return new_config, applied, rejected, confidence, reasoning

def log_changes(stats, applied, rejected, confidence, reasoning, cycle):
    sep = "─" * 60
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CHANGELOG_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{sep}\n")
        f.write(f"CYCLE #{cycle}  |  {ts}  |  Confidence: {confidence}\n")
        f.write(f"Reasoning: {reasoning}\n")
        pf  = stats.get("robot_profit_factor", "?")
        wr  = stats.get("robot_win_rate_pct", "?")
        pnl = stats.get("robot_net_pnl", "?")
        streak = f"{stats.get('robot_current_streak', 0)}x{stats.get('robot_current_streak_type', '')}"
        f.write(f"Performance: WR={wr}%  PF={pf}  PnL={pnl}  Streak={streak}\n")
        f.write(f"Balance={stats.get('account_balance','?')}  Drawdown={stats.get('account_drawdown_pct','?')}%\n")
        if applied:
            f.write("APPLIED CHANGES:\n")
            for p, v in applied.items():
                f.write(f"  {p}: {v['from']} → {v['to']}\n")
        else:
            f.write("NO CHANGES — strategy performing acceptably\n")
        if rejected:
            f.write("REJECTED:\n")
            for p, r in rejected.items():
                f.write(f"  {p}: {r}\n")
        f.write(f"{sep}\n")

# ─── MAIN ADVISOR LOOP ───────────────────────────────────────

def run_advisor():
    # Connect to MT5
    if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
        logger.error("❌ MT5 Init Failed — cannot pull history without connection")
        return

    init_config()

    logger.info("=" * 64)
    logger.info("🧠 SCURO ADAPTIVE ADVISOR — MT5-Native Edition")
    logger.info(f"   Model    : {ADVISOR_MODEL}")
    logger.info(f"   Interval : every {ADVISOR_INTERVAL} minutes")
    logger.info(f"   Symbol   : {SYMBOL} | Magic: {MAGIC_NUMBER}")
    logger.info(f"   Lookback : {HISTORY_LOOKBACK_DAYS} days of MT5 history")
    logger.info(f"   Config   : {os.path.abspath(CONFIG_JSON)}")
    logger.info(f"   Changelog: {os.path.abspath(CHANGELOG_LOG)}")
    logger.info("=" * 64)

    while True:
        logger.info("─" * 64)
        logger.info("🔄 Starting analysis cycle...")

        # ── 1. Reconnect MT5 if needed ───────────────────────
        if not mt5.terminal_info():
            logger.warning("⚠️  MT5 connection lost — reconnecting...")
            if not mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
                logger.error("❌ MT5 reconnect failed — retrying in 60s")
                time.sleep(60)
                continue

        # ── 2. Pull live stats from MT5 ──────────────────────
        stats = pull_mt5_stats()
        if stats is None:
            logger.info(f"📋 No MT5 history yet (need {MIN_CLOSED_TRADES} closed trades) — waiting...")
            time.sleep(ADVISOR_INTERVAL * 60)
            continue

        if stats.get("robot_trades", 0) < MIN_CLOSED_TRADES:
            logger.info(f"📊 Only {stats.get('robot_trades', 0)} robot trades (need {MIN_CLOSED_TRADES}) — waiting...")
            time.sleep(ADVISOR_INTERVAL * 60)
            continue

        # ── 3. Load current config ───────────────────────────
        config = load_config()
        logger.info(f"📁 Current config (cycle #{config.get('cycle', 0)}):")
        for p in PARAM_BOUNDS:
            if p in config:
                logger.info(f"     {p}: {config[p]}")

        # ── 4. Ask Llama ─────────────────────────────────────
        logger.info(f"🤖 Querying {ADVISOR_MODEL}...")
        recommendation = ask_llama(stats, config)

        if recommendation is None:
            logger.warning("⚠️  No valid recommendation — skipping cycle")
            time.sleep(ADVISOR_INTERVAL * 60)
            continue

        # ── 5. Validate & apply ──────────────────────────────
        new_config, applied, rejected, confidence, reasoning = validate_and_apply(recommendation, config)

        save_config(new_config)   # always save to bump cycle + timestamp

        if applied:
            logger.info(f"✅ Config updated [{confidence}] — {reasoning}")
            for param, change in applied.items():
                logger.info(f"   📈 {param}: {change['from']} → {change['to']}")
        else:
            logger.info(f"〰  No changes this cycle [{confidence}] — {reasoning}")

        for param, reason in rejected.items():
            logger.warning(f"   ⚠️  Rejected {param}: {reason}")

        # ── 6. Write changelog ───────────────────────────────
        log_changes(stats, applied, rejected, confidence, reasoning, new_config["cycle"])

        # ── 7. Sleep ─────────────────────────────────────────
        logger.info(f"⏳ Next analysis in {ADVISOR_INTERVAL} minutes")
        time.sleep(ADVISOR_INTERVAL * 60)

if __name__ == "__main__":
    run_advisor()
