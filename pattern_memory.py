"""
pattern_memory.py  ─  Scuro Pattern Recognition Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Learns from the bot's own closed trades on XAUUSDm.

After every closed trade it stores the 50-candle price pattern
that preceded the entry. When a new signal fires, it finds the
most similar historical patterns and returns a confidence score
based on how those past situations resolved.

This means the bot progressively gets smarter with every trade it takes.
After ~50 trades the pattern data becomes meaningful.
After ~200 trades it becomes a genuine edge.

HOW IT WORKS (plain English):
  1. Before a trade: snapshot the last 50 candles as % changes
     (normalised so the pattern is comparable at any price level)
  2. After the trade closes: save that snapshot + outcome to disk
  3. Next signal: compare current pattern to all stored patterns
  4. Find the 20 most similar past situations
  5. Ask: "of those 20, how many were wins?"
  6. Return a score 0-100 and a human-readable summary

Import in ai_context.py:
    from pattern_memory import PatternMemory
    _pattern_mem = PatternMemory()

Then inside get_ai_confidence(), add:
    pat_score, pat_summary = _pattern_mem.query(df)
    # Add pat_summary to the context string sent to the AI

And after a trade closes, call from xau.py:
    from pattern_memory import PatternMemory
    _pattern_mem = PatternMemory()   # singleton at module level
    _pattern_mem.store(df, outcome, signal_type, signal)
"""

import os
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ─── CONSTANTS ───────────────────────────────────────────────
PATTERN_FILE       = "pattern_memory.json"
PATTERN_LENGTH     = 50      # candles to snapshot before entry
SIMILARITY_THRESH  = 0.80    # minimum similarity (0-1) to count as a match
MAX_MATCHES        = 20      # how many similar patterns to consider
MIN_PATTERNS_NEEDED = 10     # don't score until we have enough history
MAX_STORED         = 500     # cap storage to avoid slow comparisons forever


class PatternMemory:
    """
    Stores and queries historical candle patterns from the bot's own trades.
    Persists to disk so memory survives restarts.
    """

    def __init__(self, filepath: str = PATTERN_FILE):
        self.filepath = filepath
        self.patterns = []   # list of stored pattern dicts
        self._load()

    # ─── PERSISTENCE ─────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath) as f:
                    self.patterns = json.load(f)
                logger.info(
                    f"🧠 Pattern memory loaded: {len(self.patterns)} patterns from {self.filepath}"
                )
            except Exception as e:
                logger.warning(f"⚠️  Pattern memory load error: {e} — starting fresh")
                self.patterns = []
        else:
            logger.info("🧠 Pattern memory: no file yet — will create on first trade")
            self.patterns = []

    def _save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.patterns, f)
        except Exception as e:
            logger.warning(f"⚠️  Pattern memory save error: {e}")

    # ─── PATTERN ENCODING ────────────────────────────────────

    @staticmethod
    def _encode(df: pd.DataFrame) -> Optional[list]:
        """
        Converts the last PATTERN_LENGTH candles into a normalised
        percentage-change vector. This makes patterns comparable
        regardless of the absolute price level gold is at.

        Returns a list of floats, or None if not enough data.
        """
        if len(df) < PATTERN_LENGTH + 1:
            return None

        closes = df['close'].values[-(PATTERN_LENGTH + 1):]

        # Percentage change between consecutive closes
        pct_changes = []
        for i in range(1, len(closes)):
            if closes[i - 1] != 0:
                pct_changes.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100)
            else:
                pct_changes.append(0.0)

        return [round(float(v), 5) for v in pct_changes]

    @staticmethod
    def _similarity(a: list, b: list) -> float:
        """
        Cosine similarity between two pattern vectors.
        Returns 0.0–1.0. Higher = more similar.

        Cosine similarity is ideal here because it measures the
        SHAPE of the pattern, not the magnitude — two patterns
        that moved the same direction at the same rhythm will score
        close to 1.0 even if one was twice as volatile.
        """
        if len(a) != len(b):
            return 0.0
        va = np.array(a, dtype=np.float64)
        vb = np.array(b, dtype=np.float64)
        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))

    # ─── PUBLIC API ───────────────────────────────────────────

    def store(
        self,
        df: pd.DataFrame,
        outcome: str,          # "WIN", "LOSS", or "BE"
        signal_type: str,      # "TREND", "DIP", "BREAKOUT"
        signal: str,           # "BUY" or "SELL"
        profit: float = 0.0,
    ):
        """
        Call this after a trade closes to store its pattern + outcome.
        Pass the M5 DataFrame that was current at the time of entry.

        Usage in xau.py (inside sync_mt5_history after outcome is known):
            _pattern_mem.store(df, outcome="WIN", signal_type="TREND",
                               signal="BUY", profit=1540.0)
        """
        pattern = self._encode(df)
        if pattern is None:
            logger.warning("⚠️  Pattern memory: not enough candles to encode — skipping store")
            return

        entry = {
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal":      signal,
            "signal_type": signal_type,
            "outcome":     outcome,
            "profit":      round(profit, 2),
            "pattern":     pattern,
        }

        self.patterns.append(entry)

        # Cap storage — drop oldest if over limit
        if len(self.patterns) > MAX_STORED:
            self.patterns = self.patterns[-MAX_STORED:]
            logger.info(f"🧠 Pattern memory trimmed to {MAX_STORED} entries")

        self._save()
        logger.info(
            f"🧠 Pattern stored: {signal_type} {signal} → {outcome} "
            f"({profit:+.2f}) | Total: {len(self.patterns)}"
        )

    def query(
        self,
        df: pd.DataFrame,
        signal: str = "",
        signal_type: str = "",
    ) -> tuple[int, str]:
        """
        Compare the current candle pattern against all stored patterns.
        Returns (score: int 0-100, summary: str).

        Score interpretation:
          0  – 30  : historically bearish / unfavourable setup
          31 – 49  : slightly unfavourable, be cautious
          50 – 64  : neutral / mixed history
          65 – 79  : historically favourable
          80 – 100 : strong historical edge

        Usage in ai_context.py:
            pat_score, pat_summary = _pattern_mem.query(df, signal, signal_type)
        """
        if len(self.patterns) < MIN_PATTERNS_NEEDED:
            msg = (
                f"Pattern memory: only {len(self.patterns)}/{MIN_PATTERNS_NEEDED} "
                f"patterns stored — not enough history yet"
            )
            logger.info(f"🧠 {msg}")
            return 50, msg   # neutral score when not enough data

        current = self._encode(df)
        if current is None:
            return 50, "Pattern memory: insufficient candle data for encoding"

        # ── Score all stored patterns by similarity ───────────
        scored = []
        for p in self.patterns:
            sim = self._similarity(current, p["pattern"])
            if sim >= SIMILARITY_THRESH:
                scored.append({
                    "similarity": sim,
                    "outcome":    p["outcome"],
                    "profit":     p["profit"],
                    "signal":     p["signal"],
                    "signal_type": p["signal_type"],
                })

        if not scored:
            msg = (
                f"Pattern memory: no matches above {SIMILARITY_THRESH:.0%} similarity "
                f"(searched {len(self.patterns)} patterns)"
            )
            logger.info(f"🧠 {msg}")
            return 50, msg   # no similar patterns found — neutral

        # ── Take the top MAX_MATCHES by similarity ────────────
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        top = scored[:MAX_MATCHES]

        wins   = sum(1 for p in top if p["outcome"] == "WIN")
        losses = sum(1 for p in top if p["outcome"] == "LOSS")
        bes    = sum(1 for p in top if p["outcome"] == "BE")
        total  = len(top)

        win_rate   = wins / total
        avg_profit = sum(p["profit"] for p in top) / total
        avg_sim    = sum(p["similarity"] for p in top) / total

        # ── Build score 0–100 ─────────────────────────────────
        # Base: win rate contribution (0–70 pts)
        base_score = win_rate * 70

        # Profitability bonus/penalty (0–20 pts)
        if avg_profit > 1000:   base_score += 20
        elif avg_profit > 500:  base_score += 12
        elif avg_profit > 0:    base_score += 6
        elif avg_profit > -500: base_score -= 6
        else:                   base_score -= 15

        # Similarity quality bonus (0–10 pts)
        base_score += avg_sim * 10

        score = max(0, min(100, int(base_score)))

        # ── Direction filter — penalise if history disagrees ──
        # e.g. current signal is BUY but most similar patterns were SELL wins
        if signal:
            direction_matches = sum(
                1 for p in top if p["signal"] == signal and p["outcome"] == "WIN"
            )
            direction_rate = direction_matches / total
            if direction_rate < 0.25:
                score = max(0, score - 15)
                logger.info(
                    f"🧠 Direction penalty: only {direction_rate:.0%} of similar "
                    f"patterns were winning {signal}s"
                )

        # ── Summary string for AI context ─────────────────────
        summary = (
            f"Pattern memory ({total} similar patterns found, "
            f"avg similarity {avg_sim:.0%}): "
            f"{wins}W / {losses}L / {bes}BE | "
            f"Historical WR: {win_rate:.0%} | "
            f"Avg P&L of similar trades: {avg_profit:+.0f} KES | "
            f"Pattern score: {score}/100"
        )

        logger.info(f"🧠 {summary}")
        return score, summary

    # ─── STATS ───────────────────────────────────────────────

    def stats(self) -> str:
        """Returns a summary of everything stored in memory."""
        if not self.patterns:
            return "Pattern memory: empty"

        total  = len(self.patterns)
        wins   = sum(1 for p in self.patterns if p["outcome"] == "WIN")
        losses = sum(1 for p in self.patterns if p["outcome"] == "LOSS")
        bes    = total - wins - losses

        by_type = {}
        for p in self.patterns:
            t = p.get("signal_type", "UNKNOWN")
            by_type.setdefault(t, {"W": 0, "L": 0, "BE": 0})
            if p["outcome"] == "WIN":   by_type[t]["W"] += 1
            elif p["outcome"] == "LOSS": by_type[t]["L"] += 1
            else:                        by_type[t]["BE"] += 1

        lines = [
            f"🧠 Pattern Memory Stats — {total} patterns stored",
            f"   Overall: {wins}W / {losses}L / {bes}BE",
        ]
        for t, counts in by_type.items():
            lines.append(f"   {t}: {counts['W']}W / {counts['L']}L / {counts['BE']}BE")

        return "\n".join(lines)
