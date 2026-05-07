"""
dashboard.py  ─  Scuro Live Trading Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads scuro_live_data.json (written by mt5_history.py every 10s)
and renders a sophisticated dark-mode dashboard.

Run:
    streamlit run dashboard.py
"""

import json
import os
import time
from datetime import datetime
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# ─── PAGE CONFIG ─────────────────────────────────────────────
st.set_page_config(
    page_title="SCURO — Live Trading Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DATA_FILE = "scuro_live_data.json"

# ─── GLOBAL STYLES ───────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap');

:root {
    --bg:        #09090f;
    --bg2:       #0f0f1a;
    --bg3:       #161628;
    --border:    #1e1e35;
    --accent:    #c8a96e;
    --accent2:   #7b68ee;
    --green:     #22d3a0;
    --red:       #f04f5f;
    --yellow:    #f5c542;
    --text:      #e4e4f0;
    --muted:     #6b6b8a;
    --mono:      'Space Mono', monospace;
    --sans:      'DM Sans', sans-serif;
}

html, body, [class*="css"] {
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
}

/* Hide streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1rem 2rem 2rem 2rem !important; max-width: 100% !important; }

/* Metric cards */
.metric-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.2rem 1.4rem;
    position: relative;
    overflow: hidden;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
}
.metric-label {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.12em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 0.4rem;
}
.metric-value {
    font-family: var(--mono);
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--text);
    line-height: 1;
}
.metric-sub {
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 0.3rem;
}
.metric-pos { color: var(--green) !important; }
.metric-neg { color: var(--red) !important; }
.metric-warn { color: var(--yellow) !important; }

/* Section headers */
.section-header {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    color: var(--accent);
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
    margin-bottom: 1rem;
    margin-top: 1.5rem;
}

/* Trade table */
.trade-row {
    display: grid;
    grid-template-columns: 80px 140px 60px 80px 90px 90px 90px 80px 90px;
    gap: 0.5rem;
    padding: 0.5rem 0.8rem;
    border-bottom: 1px solid var(--border);
    font-family: var(--mono);
    font-size: 0.72rem;
    align-items: center;
}
.trade-row:hover { background: var(--bg3); }
.trade-row.header {
    color: var(--muted);
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    border-bottom: 1px solid var(--accent);
}
.tag-buy  { background: rgba(34,211,160,0.15); color: var(--green);  padding: 2px 7px; border-radius: 4px; }
.tag-sell { background: rgba(240,79,95,0.15);  color: var(--red);    padding: 2px 7px; border-radius: 4px; }
.tag-win  { color: var(--green); }
.tag-loss { color: var(--red); }
.tag-be   { color: var(--muted); }
.tag-open { color: var(--yellow); }

/* Signal badge */
.signal-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 6px;
    font-family: var(--mono);
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.05em;
}
.signal-up   { background: rgba(34,211,160,0.2); color: var(--green); border: 1px solid var(--green); }
.signal-down { background: rgba(240,79,95,0.2);  color: var(--red);   border: 1px solid var(--red); }
.signal-flat { background: rgba(107,107,138,0.2); color: var(--muted); border: 1px solid var(--border); }

/* Config params */
.param-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.8rem;
}
.param-key   { color: var(--muted); font-family: var(--mono); font-size: 0.72rem; }
.param-value { color: var(--accent); font-family: var(--mono); font-weight: 700; }

/* Live dot */
.live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    background: var(--green);
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(34,211,160,0.4); }
    50%       { opacity: 0.7; box-shadow: 0 0 0 6px rgba(34,211,160,0); }
}

/* Streak dots */
.outcome-dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin: 0 2px;
}
.dot-win  { background: var(--green); }
.dot-loss { background: var(--red); }
.dot-be   { background: var(--muted); }

/* Open position card */
.pos-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    margin-bottom: 0.6rem;
}
.pos-card.pos-buy  { border-left: 3px solid var(--green); }
.pos-card.pos-sell { border-left: 3px solid var(--red); }

/* Scrollable table wrapper */
.table-scroll {
    max-height: 420px;
    overflow-y: auto;
    border-radius: 10px;
    border: 1px solid var(--border);
}

/* Plotly chart bg */
.js-plotly-plot { border-radius: 12px !important; }

/* Refresh bar */
.refresh-bar {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--muted);
    text-align: right;
    margin-bottom: 0.5rem;
}
</style>
""", unsafe_allow_html=True)

# ─── DATA LOADING ─────────────────────────────────────────────
@st.cache_data(ttl=10)
def load_data() -> dict | None:
    if not os.path.exists(DATA_FILE):
        return None
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def col_metric(label: str, value: str, sub: str = "", color: str = ""):
    cls = f"metric-{color}" if color else ""
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value {cls}">{value}</div>
        {"<div class='metric-sub'>" + sub + "</div>" if sub else ""}
    </div>
    """, unsafe_allow_html=True)


def outcome_dots(outcomes: list[str]) -> str:
    html = ""
    for o in outcomes:
        cls = "dot-win" if o == "WIN" else ("dot-loss" if o == "LOSS" else "dot-be")
        html += f'<span class="outcome-dot {cls}" title="{o}"></span>'
    return html


# ─── CHART BUILDERS ──────────────────────────────────────────
CHART_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Space Mono", color="#6b6b8a", size=11),
    margin=dict(l=10, r=10, t=30, b=10),
    showlegend=False,
    xaxis=dict(gridcolor="#1e1e35", linecolor="#1e1e35", showgrid=True),
    yaxis=dict(gridcolor="#1e1e35", linecolor="#1e1e35", showgrid=True),
)


def build_equity_chart(equity_curve: list, open_positions: list) -> go.Figure:
    if not equity_curve:
        return go.Figure()

    df = pd.DataFrame(equity_curve)
    df["time"] = pd.to_datetime(df["time"])

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["time"], y=df["balance"],
        mode="lines",
        line=dict(color="#c8a96e", width=2),
        fill="tozeroy",
        fillcolor="rgba(200,169,110,0.07)",
        name="Balance",
        hovertemplate="%{x|%H:%M}<br>KES %{y:,.2f}<extra></extra>",
    ))

    fig.update_layout(
        **CHART_LAYOUT,
        title=dict(text="EQUITY CURVE", font=dict(size=10, color="#6b6b8a"), x=0.01),
        height=220,
        yaxis=dict(**CHART_LAYOUT["yaxis"], tickformat=",.0f"),
    )
    return fig


def build_pnl_bar(closed_trades: list) -> go.Figure:
    if not closed_trades:
        return go.Figure()

    df = pd.DataFrame(closed_trades)
    df = df[df.get("magic", 0) != 0]  # all trades
    df["close_time"] = pd.to_datetime(df["close_time"])
    df["hour"]       = df["close_time"].dt.floor("H")
    hourly           = df.groupby("hour")["profit"].sum().reset_index()

    colors = ["#22d3a0" if v >= 0 else "#f04f5f" for v in hourly["profit"]]

    fig = go.Figure(go.Bar(
        x=hourly["hour"], y=hourly["profit"],
        marker_color=colors,
        hovertemplate="%{x|%H:%M}<br>KES %{y:+,.2f}<extra></extra>",
    ))
    fig.update_layout(
        **CHART_LAYOUT,
        title=dict(text="HOURLY P&L", font=dict(size=10, color="#6b6b8a"), x=0.01),
        height=220,
        yaxis=dict(**CHART_LAYOUT["yaxis"], tickformat="+,.0f"),
    )
    return fig


def build_win_rate_gauge(wr: float, pf: float) -> go.Figure:
    fig = make_subplots(
        rows=1, cols=2,
        specs=[[{"type": "indicator"}, {"type": "indicator"}]],
    )

    wr_color = "#22d3a0" if wr >= 55 else ("#f5c542" if wr >= 45 else "#f04f5f")
    pf_color = "#22d3a0" if pf >= 1.3 else ("#f5c542" if pf >= 1.0 else "#f04f5f")

    fig.add_trace(go.Indicator(
        mode="gauge+number",
        value=wr,
        number=dict(suffix="%", font=dict(family="Space Mono", color=wr_color, size=24)),
        title=dict(text="WIN RATE", font=dict(family="Space Mono", color="#6b6b8a", size=10)),
        gauge=dict(
            axis=dict(range=[0, 100], tickcolor="#1e1e35"),
            bar=dict(color=wr_color, thickness=0.25),
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            steps=[
                dict(range=[0, 45],   color="rgba(240,79,95,0.15)"),
                dict(range=[45, 55],  color="rgba(245,197,66,0.15)"),
                dict(range=[55, 100], color="rgba(34,211,160,0.15)"),
            ],
            threshold=dict(line=dict(color="#c8a96e", width=2), thickness=0.75, value=55),
        ),
    ), row=1, col=1)

    fig.add_trace(go.Indicator(
        mode="gauge+number",
        value=pf,
        number=dict(font=dict(family="Space Mono", color=pf_color, size=24)),
        title=dict(text="PROFIT FACTOR", font=dict(family="Space Mono", color="#6b6b8a", size=10)),
        gauge=dict(
            axis=dict(range=[0, 3], tickcolor="#1e1e35"),
            bar=dict(color=pf_color, thickness=0.25),
            bgcolor="rgba(0,0,0,0)",
            borderwidth=0,
            steps=[
                dict(range=[0, 1.0], color="rgba(240,79,95,0.15)"),
                dict(range=[1.0, 1.3], color="rgba(245,197,66,0.15)"),
                dict(range=[1.3, 3],   color="rgba(34,211,160,0.15)"),
            ],
            threshold=dict(line=dict(color="#c8a96e", width=2), thickness=0.75, value=1.3),
        ),
    ), row=1, col=2)

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Space Mono", color="#6b6b8a"),
        height=200,
        margin=dict(l=20, r=20, t=30, b=10),
    )
    return fig


def build_drawdown_chart(account: dict, equity_curve: list) -> go.Figure:
    if not equity_curve:
        return go.Figure()

    df = pd.DataFrame(equity_curve)
    df["time"]     = pd.to_datetime(df["time"])
    df["peak"]     = df["balance"].cummax()
    df["drawdown"] = (df["peak"] - df["balance"]) / df["peak"] * 100

    fig = go.Figure(go.Scatter(
        x=df["time"], y=-df["drawdown"],
        mode="lines",
        line=dict(color="#f04f5f", width=1.5),
        fill="tozeroy",
        fillcolor="rgba(240,79,95,0.1)",
        hovertemplate="%{x|%H:%M}<br>DD: %{y:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        **CHART_LAYOUT,
        title=dict(text="DRAWDOWN %", font=dict(size=10, color="#6b6b8a"), x=0.01),
        height=180,
        yaxis=dict(**CHART_LAYOUT["yaxis"], tickformat=".1f", ticksuffix="%"),
    )
    return fig


# ─── MAIN RENDER ─────────────────────────────────────────────
def render():
    data = load_data()

    # ── Header ──────────────────────────────────────────────
    h_left, h_mid, h_right = st.columns([2, 3, 2])
    with h_left:
        st.markdown(
            '<div style="font-family:\'Space Mono\',monospace;font-size:1.3rem;font-weight:700;'
            'color:#c8a96e;letter-spacing:0.1em;">⚡ SCURO</div>'
            '<div style="font-family:\'Space Mono\',monospace;font-size:0.6rem;'
            'color:#6b6b8a;letter-spacing:0.2em;">XAUUSD SCALPER — LIVE DASHBOARD</div>',
            unsafe_allow_html=True,
        )
    with h_mid:
        if data:
            tick  = data.get("live_tick", {})
            bid   = tick.get("bid", "—")
            ask   = tick.get("ask", "—")
            sprd  = tick.get("spread", "—")
            ind   = data.get("live_indicators", {})
            rsi   = ind.get("rsi", "—")
            trend = ind.get("ema_trend", "—")
            h1    = data.get("h1_trend", "—")
            t_cls = "signal-up" if trend == "UP" else ("signal-down" if trend == "DOWN" else "signal-flat")
            h_cls = "signal-up" if h1 == "UP" else ("signal-down" if h1 == "DOWN" else "signal-flat")
            st.markdown(
                f'<div style="text-align:center;padding-top:0.3rem;">'
                f'<span style="font-family:Space Mono;font-size:1.5rem;font-weight:700;color:#e4e4f0;">'
                f'{bid} / {ask}</span>'
                f'<span style="font-family:Space Mono;font-size:0.7rem;color:#6b6b8a;margin-left:10px;">'
                f'spread {sprd}</span><br>'
                f'<span class="signal-badge {t_cls}">M5 {trend}</span>&nbsp;'
                f'<span class="signal-badge {h_cls}">H1 {h1}</span>&nbsp;'
                f'<span style="font-family:Space Mono;font-size:0.75rem;color:#6b6b8a;">RSI {rsi}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
    with h_right:
        if data:
            refreshed = data.get("meta", {}).get("refreshed_at", "")
            acct      = data.get("account", {})
            acct_type = acct.get("type", "").upper()
            acct_name = acct.get("name", "")
            st.markdown(
                f'<div style="text-align:right;font-family:Space Mono;font-size:0.65rem;color:#6b6b8a;">'
                f'<span class="live-dot"></span>LIVE&nbsp;&nbsp;{refreshed}<br>'
                f'<span style="color:#c8a96e;">{acct_name}</span> · {acct_type}'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)

    if not data:
        st.error(
            f"⚠️  **{DATA_FILE} not found.**  "
            "Start the data feed first:\n```\npython mt5_history.py\n```"
        )
        st.stop()

    stats   = data.get("stats", {})
    account = data.get("account", {})
    today   = data.get("today", {})
    cfg     = data.get("advisor_config", {})
    open_pos = data.get("open_positions", [])
    closed   = data.get("closed_trades", [])

    # ── KPI Row ─────────────────────────────────────────────
    k1, k2, k3, k4, k5, k6, k7 = st.columns(7)

    balance = account.get("balance", 0)
    equity  = account.get("equity", 0)
    dd_pct  = account.get("drawdown_pct", 0)
    wr      = stats.get("robot_win_rate_pct", 0)
    pf      = stats.get("robot_profit_factor", 0)
    net_pnl = stats.get("robot_net_pnl", 0)
    trades  = stats.get("robot_trades", 0)
    today_pnl = today.get("pnl", 0)
    streak  = stats.get("robot_current_streak", 0)
    s_type  = stats.get("robot_current_streak_type", "")

    with k1:
        bal_color = "pos" if balance > 30000 else "neg"
        col_metric("Balance", f"{balance:,.0f}", f"{account.get('currency','KES')}", bal_color)
    with k2:
        eq_color = "pos" if equity >= balance else "neg"
        col_metric("Equity", f"{equity:,.0f}", f"Float: {equity - balance:+,.0f}", eq_color)
    with k3:
        pnl_color = "pos" if net_pnl >= 0 else "neg"
        col_metric("Net P&L", f"{net_pnl:+,.0f}", f"{trades} trades", pnl_color)
    with k4:
        today_color = "pos" if today_pnl >= 0 else "neg"
        col_metric("Today P&L", f"{today_pnl:+,.0f}", f"{today.get('trades',0)} trades", today_color)
    with k5:
        wr_color = "pos" if wr >= 55 else ("warn" if wr >= 45 else "neg")
        col_metric("Win Rate", f"{wr:.1f}%", f"W:{stats.get('robot_wins',0)} L:{stats.get('robot_losses',0)}", wr_color)
    with k6:
        pf_color = "pos" if pf >= 1.3 else ("warn" if pf >= 1.0 else "neg")
        col_metric("Profit Factor", f"{pf:.3f}", "target: 1.30+", pf_color)
    with k7:
        dd_color = "pos" if dd_pct < 10 else ("warn" if dd_pct < 25 else "neg")
        streak_s_color = "neg" if s_type == "LOSS" else "pos"
        col_metric("Drawdown", f"{dd_pct:.1f}%",
                   f'<span class="{streak_s_color}">{streak}x{s_type}</span>', dd_color)

    # ── Charts Row ──────────────────────────────────────────
    st.markdown('<div class="section-header">PERFORMANCE CHARTS</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        st.plotly_chart(
            build_equity_chart(data.get("equity_curve", []), open_pos),
            use_container_width=True, config={"displayModeBar": False},
            key="chart_equity",
        )
    with c2:
        st.plotly_chart(
            build_pnl_bar(closed),
            use_container_width=True, config={"displayModeBar": False},
            key="chart_pnl_bar",
        )
    with c3:
        st.plotly_chart(
            build_win_rate_gauge(wr, pf),
            use_container_width=True, config={"displayModeBar": False},
            key="chart_gauges",
        )

    # ── Drawdown ─────────────────────────────────────────────
    st.plotly_chart(
        build_drawdown_chart(account, data.get("equity_curve", [])),
        use_container_width=True, config={"displayModeBar": False},
        key="chart_drawdown",
    )

    # ── Open Positions + Advisor Config ─────────────────────
    op_col, cfg_col = st.columns([3, 2])

    with op_col:
        st.markdown('<div class="section-header">OPEN POSITIONS</div>', unsafe_allow_html=True)
        if not open_pos:
            st.markdown(
                '<div style="color:#6b6b8a;font-family:Space Mono;font-size:0.8rem;'
                'padding:1rem;border:1px solid #1e1e35;border-radius:8px;text-align:center;">'
                'NO OPEN POSITIONS</div>',
                unsafe_allow_html=True,
            )
        else:
            for pos in open_pos:
                d = pos["direction"]
                pnl = pos["float_pnl"]
                pnl_cls = "metric-pos" if pnl >= 0 else "metric-neg"
                dir_tag = f'<span class="tag-{"buy" if d=="BUY" else "sell"}">{d}</span>'
                robot_tag = "🤖" if pos.get("magic") == MAGIC_NUMBER else "👤"
                st.markdown(f"""
                <div class="pos-card pos-{"buy" if d=="BUY" else "sell"}">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            {robot_tag} {dir_tag}
                            <span style="font-family:Space Mono;font-size:0.75rem;color:#6b6b8a;margin-left:8px;">
                                #{pos['ticket']} · {pos['volume']}lot
                            </span>
                        </div>
                        <div class="metric-value {pnl_cls}" style="font-size:1.1rem;">
                            {pnl:+,.2f}
                        </div>
                    </div>
                    <div style="font-family:Space Mono;font-size:0.68rem;color:#6b6b8a;margin-top:0.4rem;">
                        Entry: {pos['entry_price']} &nbsp;·&nbsp;
                        SL: {pos.get('sl') or '—'} &nbsp;·&nbsp;
                        TP: {pos.get('tp') or '—'} &nbsp;·&nbsp;
                        {pos['hold_mins']:.0f}m open
                    </div>
                </div>
                """, unsafe_allow_html=True)

    with cfg_col:
        st.markdown('<div class="section-header">ADVISOR CONFIG</div>', unsafe_allow_html=True)
        if cfg:
            params = [
                ("lot_size",         cfg.get("accumulation_lot")),
                ("reward_ratio",     cfg.get("reward_ratio")),
                ("rsi_buy",          cfg.get("rsi_buy_threshold")),
                ("rsi_sell",         cfg.get("rsi_sell_threshold")),
                ("sl_atr_mult",      cfg.get("sl_atr_mult")),
                ("trail_atr_mult",   cfg.get("trail_atr_mult")),
                ("ema_fast",         cfg.get("ema_fast")),
                ("ema_slow",         cfg.get("ema_slow")),
                ("max_positions",    cfg.get("max_open_positions")),
                ("cooldown_mins",    cfg.get("cooldown_mins")),
            ]
            for k, v in params:
                if v is not None:
                    st.markdown(
                        f'<div class="param-row">'
                        f'<span class="param-key">{k}</span>'
                        f'<span class="param-value">{v}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            cycle = cfg.get("cycle", "—")
            reason = cfg.get("update_reason", "")
            updated = cfg.get("last_updated", "")
            st.markdown(
                f'<div style="margin-top:0.8rem;font-family:Space Mono;font-size:0.62rem;color:#6b6b8a;">'
                f'Cycle #{cycle} · {updated}<br>'
                f'<span style="color:#c8a96e;">{reason[:60]}{"..." if len(reason) > 60 else ""}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── Trade History Table ──────────────────────────────────
    st.markdown('<div class="section-header">CLOSED TRADE HISTORY</div>', unsafe_allow_html=True)

    if closed:
        # Filter controls
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            filter_magic = st.selectbox(
                "Filter", ["All Trades", "Robot Only", "Manual Only"],
                label_visibility="collapsed",
            )
        with fc2:
            filter_dir = st.selectbox(
                "Direction", ["All Directions", "BUY", "SELL"],
                label_visibility="collapsed",
            )
        with fc3:
            filter_outcome = st.selectbox(
                "Outcome", ["All Outcomes", "WIN", "LOSS", "BE"],
                label_visibility="collapsed",
            )

        df_trades = pd.DataFrame(closed)

        if filter_magic == "Robot Only":
            df_trades = df_trades[df_trades.get("magic", 0) == MAGIC_NUMBER]
        elif filter_magic == "Manual Only":
            df_trades = df_trades[df_trades.get("magic", 0) != MAGIC_NUMBER]

        if filter_dir != "All Directions":
            df_trades = df_trades[df_trades["direction"] == filter_dir]
        if filter_outcome != "All Outcomes":
            df_trades = df_trades[df_trades["outcome"] == filter_outcome]

        # Build HTML table
        hdr = (
            '<div class="trade-row header">'
            '<span>Ticket</span><span>Close Time</span><span>Dir</span>'
            '<span>Lot</span><span>Entry</span><span>Close</span>'
            '<span>SL</span><span>TP</span><span>P&L</span>'
            '</div>'
        )
        rows_html = hdr
        for _, r in df_trades.head(200).iterrows():
            outcome = r.get("outcome", "")
            pnl     = r.get("profit", 0)
            d       = r.get("direction", "")
            try: pnl_str = f"{float(pnl):+,.2f}"
            except: pnl_str = str(pnl)
            pnl_cls = "tag-win" if outcome == "WIN" else ("tag-loss" if outcome == "LOSS" else "tag-be")
            dir_tag = f'<span class="tag-{"buy" if d == "BUY" else "sell"}">{d}</span>'
            robot_icon = "🤖" if r.get("magic") == MAGIC_NUMBER else "👤"
            rows_html += (
                f'<div class="trade-row">'
                f'<span style="color:#6b6b8a;">{robot_icon} {str(r.get("ticket",""))[:8]}</span>'
                f'<span style="color:#6b6b8a;font-size:0.65rem;">{r.get("close_time","")}</span>'
                f'<span>{dir_tag}</span>'
                f'<span style="color:#6b6b8a;">{r.get("volume","")}</span>'
                f'<span>{r.get("entry_price","")}</span>'
                f'<span>{r.get("close_price","")}</span>'
                f'<span style="color:#6b6b8a;">{r.get("sl") or "—"}</span>'
                f'<span style="color:#6b6b8a;">{r.get("tp") or "—"}</span>'
                f'<span class="{pnl_cls}" style="font-weight:700;">{pnl_str}</span>'
                f'</div>'
            )

        st.markdown(
            f'<div class="table-scroll">{rows_html}</div>',
            unsafe_allow_html=True,
        )

        # Summary row
        total_pnl = df_trades["profit"].astype(float).sum() if not df_trades.empty else 0
        st.markdown(
            f'<div style="font-family:Space Mono;font-size:0.7rem;color:#6b6b8a;'
            f'text-align:right;margin-top:0.4rem;">'
            f'Showing {min(len(df_trades),200)} of {len(df_trades)} trades · '
            f'Filtered P&L: <span style="color:{"#22d3a0" if total_pnl >= 0 else "#f04f5f"};font-weight:700;">'
            f'{total_pnl:+,.2f}</span></div>',
            unsafe_allow_html=True,
        )

    # ── Statistics Deep-Dive ─────────────────────────────────
    st.markdown('<div class="section-header">STATISTICS</div>', unsafe_allow_html=True)
    s1, s2, s3, s4 = st.columns(4)

    with s1:
        col_metric("Gross Profit", f"{stats.get('robot_gross_profit',0):,.2f}")
        col_metric("Avg Win", f"{stats.get('robot_avg_win',0):+,.2f}", color="pos")
    with s2:
        col_metric("Gross Loss", f"{stats.get('robot_gross_loss',0):,.2f}")
        col_metric("Avg Loss", f"-{abs(stats.get('robot_avg_loss',0)):,.2f}", color="neg")
    with s3:
        col_metric("Best Trade", f"{stats.get('robot_best_trade',0):+,.2f}", color="pos")
        col_metric("Long Trades",
                   f"{stats.get('robot_trades',0)}",
                   f"W:{stats.get('robot_wins',0)} L:{stats.get('robot_losses',0)}")
    with s4:
        col_metric("Worst Trade", f"{stats.get('robot_worst_trade',0):+,.2f}", color="neg")
        # Last 5 outcomes
        last5 = stats.get("robot_last_5_outcomes", [])
        dots  = outcome_dots(last5)
        st.markdown(
            f'<div class="metric-card">'
            f'<div class="metric-label">Last 5 Outcomes</div>'
            f'<div style="margin-top:0.6rem;">{dots}</div>'
            f'<div class="metric-sub">{" · ".join(last5)}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Footer ───────────────────────────────────────────────
    refreshed = data.get("meta", {}).get("refreshed_at", "")
    st.markdown(
        f'<div class="refresh-bar" style="margin-top:2rem;">'
        f'<span class="live-dot"></span>Auto-refreshes every 10s · '
        f'Last data: {refreshed} · Data source: MT5 terminal history'
        f'</div>',
        unsafe_allow_html=True,
    )


# ─── AUTO-REFRESH ─────────────────────────────────────────────
render()

# Auto-refresh via st.rerun
time.sleep(10)
st.rerun()
