"""
ai_context.py  ─  Scuro AI Context Builder & Confidence Scorer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces the raw CONFIRM/REJECT prompt in xau.py with:
  1. Enriched context — session P&L, open positions, last 5 outcomes,
     drawdown, ADX, signal quality score
  2. Confidence score 1–10 — AI returns a number, not just a word
  3. Minimum threshold gate — only trades scoring >= MIN_CONFIDENCE fire
  4. Fallback — if parsing fails, falls back to rule-based confirm

Import in xau.py:
    from ai_context import build_ai_context, get_ai_confidence, CONFIDENCE_THRESHOLD
    
Replace get_dual_ai_consensus() call with:
    ok, reason = get_ai_confidence(
        signal, signal_type, rsi_vals, adx_val, quality_score,
        h1_trend, h4_trend, news, chart_text,
        session_pnl, open_count, last_5_outcomes, account_drawdown_pct
    )
"""

import os
import re
import time
import json
import logging
import requests
from dotenv import load_dotenv
from pattern_memory import PatternMemory

# Singleton — one shared instance across all calls
_pattern_mem = PatternMemory()

load_dotenv()
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
LOGIC_MODEL    = "llama-3.3-70b-versatile"
SECONDARY_MODEL = "llama-3.1-8b-instant"

logger = logging.getLogger(__name__)

# ─── CONFIDENCE GATE ─────────────────────────────────────────
# Primary AI must score >= this to proceed to secondary check.
# Secondary AI must score >= this to confirm the trade.
# Raise this to be more selective. Range: 1–10.
MIN_CONFIDENCE     = 7      # primary agent minimum
MIN_CONFIDENCE_SEC = 6      # secondary agent minimum (slightly looser)

# Quality score (from filters.py) must be >= this or trade is blocked
# regardless of AI confidence
MIN_QUALITY_SCORE  = 40


# ─── CONTEXT BUILDER ─────────────────────────────────────────

def build_ai_context(
    signal: str,
    signal_type: str,
    rsi_vals: dict,
    adx_val: float,
    quality_score: int,
    h1_trend: str,
    h4_trend: str,
    news: str,
    chart_text: str,
    session_pnl: float,
    open_count: int,
    last_5_outcomes: list,
    drawdown_pct: float,
) -> str:
    """
    Builds a rich context string for the AI prompt.
    This replaces the minimal RSI/H1/H4/News prompt that was rubber-stamping.
    """
    outcomes_str = " → ".join(last_5_outcomes) if last_5_outcomes else "No data"
    recent_losses = last_5_outcomes.count("LOSS") if last_5_outcomes else 0

    # Session health summary
    if session_pnl < -5000:
        session_health = "CRITICAL — large drawdown today"
    elif session_pnl < -2000:
        session_health = "POOR — losing session"
    elif session_pnl < 0:
        session_health = "SLIGHTLY NEGATIVE"
    elif session_pnl > 3000:
        session_health = "GOOD — profitable session"
    else:
        session_health = "NEUTRAL"

    context = f"""
SIGNAL REQUEST: {signal} [{signal_type}]

── MARKET CONDITIONS ──────────────────────────
Chart summary : {chart_text}
H1 trend      : {h1_trend}
H4 trend      : {h4_trend}
ADX strength  : {adx_val:.1f} {'(TRENDING)' if adx_val >= 20 else '(RANGING — LOW CONFIDENCE)'}
RSI values    : {json.dumps(rsi_vals)}
News          : {news}

── SESSION CONTEXT ────────────────────────────
Session P&L        : {session_pnl:+.0f} KES  [{session_health}]
Open positions     : {open_count}
Account drawdown   : {drawdown_pct:.1f}%
Last 5 outcomes    : {outcomes_str}
Recent loss count  : {recent_losses}/5

── SIGNAL QUALITY ─────────────────────────────
Quality score : {quality_score}/100  {'⚠️ LOW' if quality_score < 40 else '✅ OK' if quality_score < 70 else '🔥 HIGH'}

── SIGNAL TYPE NOTES ──────────────────────────
{_signal_type_note(signal_type, signal)}
"""
    return context.strip()


def _signal_type_note(signal_type: str, signal: str) -> str:
    if signal_type == "DIP":
        return (
            "DIP-BUY: H1+H4 uptrend intact, price pulled below H1 EMA50, RSI recovering. "
            "Confirm only if dip looks exhausted and momentum is clearly reversing upward."
        )
    elif signal_type == "BREAKOUT":
        return (
            "H1 STRUCTURAL BREAKOUT: price just closed above/below the 10-bar swing level "
            "with H4 confirmation. This is a high-conviction trend entry. "
            "Reject ONLY if news risk is high or RSI is already extreme (>78 BUY / <22 SELL)."
        )
    else:
        return (
            "TREND signal: M5 EMA cross + RSI in zone + H1/H4 aligned. "
            "Be more skeptical in ranging markets (ADX < 20). "
            "Reject if recent losses > 3 AND session P&L is very negative."
        )


# ─── CONFIDENCE SCORER ───────────────────────────────────────

def _parse_confidence(text: str) -> int | None:
    """
    Extracts confidence score from AI response.
    Handles: "7", "CONFIDENCE: 8", "score: 6/10", "I give this a 7 out of 10"
    Returns None if unparseable.
    """
    text = text.strip().upper()

    # Direct REJECT handling — treat as confidence 0
    if "REJECT" in text and not any(c.isdigit() for c in text):
        return 0

    # Look for explicit score patterns
    patterns = [
        r'CONFIDENCE[:\s]+(\d+)',
        r'SCORE[:\s]+(\d+)',
        r'(\d+)\s*/\s*10',
        r'RATING[:\s]+(\d+)',
        r'^(\d+)$',                  # bare number
        r'\b([1-9]|10)\b',           # any single digit 1-9 or 10
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 10:
                return val

    return None


def _rule_based_confidence(
    signal: str,
    signal_type: str,
    h1_trend: str,
    h4_trend: str,
    rsi_vals: dict,
    adx_val: float,
    recent_losses: int,
    session_pnl: float,
) -> int:
    """
    Fallback rule-based confidence when AI is unavailable.
    Returns 1–10.
    """
    score = 5  # neutral start

    rsi_14 = rsi_vals.get("rsi_14", 50)

    if signal == "BUY":
        if h1_trend == "UP":   score += 1
        if h4_trend == "UP":   score += 1
        if rsi_14 > 50:        score += 1
        if adx_val >= 25:      score += 1
    elif signal == "SELL":
        if h1_trend == "DOWN": score += 1
        if h4_trend == "DOWN": score += 1
        if rsi_14 < 50:        score += 1
        if adx_val >= 25:      score += 1

    # Penalise bad session
    if recent_losses >= 3:     score -= 2
    if session_pnl < -5000:    score -= 2
    elif session_pnl < -2000:  score -= 1

    # Breakout bonus
    if signal_type == "BREAKOUT": score += 1

    return max(1, min(10, score))


# ─── MAIN ENTRY POINT ────────────────────────────────────────

def get_ai_confidence(
    signal: str,
    signal_type: str,
    rsi_vals: dict,
    adx_val: float,
    quality_score: int,
    h1_trend: str,
    h4_trend: str,
    news: str,
    chart_text: str,
    session_pnl: float,
    open_count: int,
    last_5_outcomes: list,
    drawdown_pct: float,
    budgeter=None,
    limiter=None,
    df=None,               # M5 DataFrame — needed for pattern memory query
) -> tuple[bool, str]:
    """
    Returns (confirmed: bool, reason_string: str).

    Drop-in replacement for get_dual_ai_consensus() in xau.py.
    Now returns a confidence score instead of CONFIRM/REJECT.

    The trade fires only if:
      1. quality_score >= MIN_QUALITY_SCORE      (filters.py gate)
      2. primary AI confidence >= MIN_CONFIDENCE
      3. secondary AI confidence >= MIN_CONFIDENCE_SEC
    """
    recent_losses = last_5_outcomes.count("LOSS") if last_5_outcomes else 0

    # ── Pattern memory query ─────────────────────────────────
    pat_score, pat_summary = (50, "Pattern memory: no df provided")
    if df is not None:
        pat_score, pat_summary = _pattern_mem.query(df, signal, signal_type)

    # Hard block: strong historical evidence AGAINST this setup
    if pat_score < 25 and len(_pattern_mem.patterns) >= 20:
        logger.info(
            f"⛔ Pattern memory blocked trade: score={pat_score}/100 — "
            f"historical edge strongly negative"
        )
        return False, f"PATTERN:{pat_score}/100 historical edge negative"

    # ── Hard quality gate (no AI needed) ─────────────────────
    if quality_score < MIN_QUALITY_SCORE:
        logger.info(
            f"⛔ Quality gate blocked trade: score={quality_score} < {MIN_QUALITY_SCORE}"
        )
        return False, f"QUALITY:{quality_score} below threshold"

    # ── Build context ─────────────────────────────────────────
    context = build_ai_context(
        signal, signal_type, rsi_vals, adx_val, quality_score,
        h1_trend, h4_trend, news, chart_text,
        session_pnl, open_count, last_5_outcomes, drawdown_pct,
    )

    # ── AI budget check ───────────────────────────────────────
    if budgeter and not budgeter.can_call_ai():
        conf = _rule_based_confidence(
            signal, signal_type, h1_trend, h4_trend,
            rsi_vals, adx_val, recent_losses, session_pnl
        )
        confirmed = conf >= MIN_CONFIDENCE
        logger.warning(
            f"⚠️  AI budget low — rule-based confidence: {conf}/10 "
            f"→ {'CONFIRM' if confirmed else 'REJECT'}"
        )
        return confirmed, f"RULE:{conf}/10"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    # ── Primary agent ─────────────────────────────────────────
    primary_conf = None
    if limiter: limiter.wait()

    primary_prompt = (
        f"{context}\n\n"
        f"Analyse this {signal_type} {signal} signal on XAUUSDm.\n"
        f"Reply with ONLY a confidence score from 1 to 10.\n"
        f"1 = strong reject, 5 = neutral, 10 = strong confirm.\n"
        f"Consider session health and recent losses in your score.\n"
        f"Reply format: just the number, nothing else."
    )

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json={
                "model": LOGIC_MODEL,
                "messages": [{"role": "user", "content": primary_prompt}],
                "max_tokens": 10,
                "temperature": 0.1,
            },
            timeout=15,
        )
        data = resp.json()
        if "choices" in data:
            raw = data["choices"][0]["message"]["content"].strip()
            primary_conf = _parse_confidence(raw)
            if budgeter: budgeter.record_call()
            logger.info(f"🤖 Primary agent confidence: {primary_conf}/10 (raw: '{raw}')")
        else:
            logger.warning(f"⚠️  Primary agent error: {data.get('error', {})}")
    except Exception as e:
        logger.error(f"❌ Primary agent failed: {e}")

    # Fallback if primary failed
    if primary_conf is None:
        primary_conf = _rule_based_confidence(
            signal, signal_type, h1_trend, h4_trend,
            rsi_vals, adx_val, recent_losses, session_pnl
        )
        logger.info(f"🔄 Primary fallback rule confidence: {primary_conf}/10")

    if primary_conf < MIN_CONFIDENCE:
        logger.info(
            f"⛔ Primary agent scored {primary_conf}/10 — "
            f"below threshold {MIN_CONFIDENCE} — skipping secondary"
        )
        return False, f"L:{primary_conf}/10 S:SKIPPED"

    # ── Secondary agent ───────────────────────────────────────
    secondary_conf = None
    if limiter: limiter.wait()

    secondary_prompt = (
        f"XAUUSDm {signal} [{signal_type}] | "
        f"H1:{h1_trend} H4:{h4_trend} ADX:{adx_val:.1f} | "
        f"Session P&L:{session_pnl:+.0f} | Last 5:{' '.join(last_5_outcomes[-5:]) if last_5_outcomes else 'none'}\n"
        f"Confidence score 1-10 only. Just the number."
    )

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json={
                "model": SECONDARY_MODEL,
                "messages": [{"role": "user", "content": secondary_prompt}],
                "max_tokens": 10,
                "temperature": 0.1,
            },
            timeout=15,
        )
        data = resp.json()
        if "choices" in data:
            raw = data["choices"][0]["message"]["content"].strip()
            secondary_conf = _parse_confidence(raw)
            if budgeter: budgeter.record_call()
            logger.info(f"🤖 Secondary agent confidence: {secondary_conf}/10 (raw: '{raw}')")
    except Exception as e:
        logger.warning(f"⚠️  Secondary agent failed: {e} — primary decides alone")

    if secondary_conf is None:
        secondary_conf = primary_conf  # primary decides alone

    confirmed = (
        primary_conf   >= MIN_CONFIDENCE and
        secondary_conf >= MIN_CONFIDENCE_SEC
    )

    reason = f"L:{primary_conf}/10 S:{secondary_conf}/10"
    logger.info(
        f"{'✅' if confirmed else '⛔'} AI consensus: {reason} | "
        f"thresholds: {MIN_CONFIDENCE}/{MIN_CONFIDENCE_SEC}"
    )
    return confirmed, reason
