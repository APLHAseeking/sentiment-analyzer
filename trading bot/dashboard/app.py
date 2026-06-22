"""Streamlit dashboard for the regime-aware trading system.

Launch with:
    cd "trading bot"
    streamlit run dashboard/app.py

Reads from dashboard_state.json (written by the orchestration loop) and
from trading.db (for historical data). Does not import any live trading
modules — pure read-only visualization.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

# Add parent dir so we can import db helpers
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dashboard.data_store import DashboardStore
from system.paths import resolve

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------

DB_PATH = resolve(os.environ.get("DB_PATH", "trading.db"))
STATE_PATH = resolve(os.environ.get("DASHBOARD_STATE", "dashboard_state.json"))
REFRESH_SECS = int(os.environ.get("DASHBOARD_REFRESH", "300"))

REGIME_COLORS = {
    "crash": "#d32f2f",
    "deep-bear": "#e64a19",
    "bear": "#f57c00",
    "neutral": "#fbc02d",
    "bull": "#388e3c",
    "euphoria": "#1976d2",
    "melt-up": "#7b1fa2",
    "unknown": "#9e9e9e",
}

# -------------------------------------------------------------------
# Data loading helpers
# -------------------------------------------------------------------

def _load_state() -> dict:
    store = DashboardStore(STATE_PATH)
    return store.read()


def _db_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def _portfolio_log() -> pd.DataFrame:
    df = _db_query("SELECT date, total_nav FROM portfolio_log ORDER BY date ASC")
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _regime_history() -> pd.DataFrame:
    df = _db_query(
        "SELECT date, regime_label, confidence, is_stable FROM regime_log ORDER BY date ASC"
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def _open_positions() -> pd.DataFrame:
    return _db_query(
        "SELECT ticker, entry_price, shares, position_pct, entry_date, rationale FROM positions"
    )


def _closed_positions() -> pd.DataFrame:
    df = _db_query(
        """SELECT ticker, entry_date, exit_date, entry_price, exit_price,
                  realized_pnl, exit_reason
           FROM closed_positions
           ORDER BY exit_date DESC
           LIMIT 50"""
    )
    return df


def _risk_events() -> pd.DataFrame:
    return _db_query(
        "SELECT event_type, description, logged_at FROM risk_events ORDER BY logged_at DESC LIMIT 20"
    )

# -------------------------------------------------------------------
# Page layout
# -------------------------------------------------------------------

st.set_page_config(
    page_title="Congressional Trading Bot — Dashboard",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Regime-Aware Congressional Trading Bot")

# Auto-refresh
st.markdown(
    f'<meta http-equiv="refresh" content="{REFRESH_SECS}">',
    unsafe_allow_html=True,
)

state = _load_state()
last_updated = state.get("last_updated", "Never")
st.caption(f"Last updated: {last_updated}  |  Refresh: every {REFRESH_SECS}s")

# -------------------------------------------------------------------
# TOP METRICS ROW
# -------------------------------------------------------------------

col1, col2, col3, col4, col5, col6 = st.columns(6)

regime = state.get("regime", {})
portfolio = state.get("portfolio", {})
risk = state.get("risk", {})

regime_label = regime.get("label", "unknown")
regime_color = REGIME_COLORS.get(regime_label, "#9e9e9e")
stability_icon = "✅" if regime.get("is_stable") else "⚠️"
lock_icon = "🔒" if risk.get("lock_file_exists") else ""

with col1:
    st.metric("Regime", f"{stability_icon} {regime_label.upper()}")
with col2:
    st.metric("Confidence", f"{regime.get('confidence', 0):.0%}")
with col3:
    st.metric("Equity (Paper)", f"${portfolio.get('equity', 0):,.0f}")
with col4:
    st.metric("Cash", f"${portfolio.get('cash', 0):,.0f}")
with col5:
    risk_state = risk.get("state", "unknown")
    st.metric("Risk State", f"{lock_icon} {risk_state}")
with col6:
    st.metric("N Regimes", regime.get("n_regimes", 0))

# -------------------------------------------------------------------
# REGIME CONFIDENCE BAR
# -------------------------------------------------------------------

posteriors = regime.get("posteriors", [])
if posteriors:
    st.subheader("Regime Posterior Distribution")
    n = len(posteriors)
    from system.config import settings as _cfg
    label_map = _cfg.regime.label_maps.get(n, [f"regime_{i}" for i in range(n)])
    bar_df = pd.DataFrame({"Regime": label_map[:len(posteriors)], "Probability": posteriors})
    fig = px.bar(bar_df, x="Regime", y="Probability",
                 color="Regime",
                 color_discrete_map={l: REGIME_COLORS.get(l, "#9e9e9e") for l in label_map},
                 range_y=[0, 1])
    fig.update_layout(showlegend=False, margin=dict(t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)

# -------------------------------------------------------------------
# EQUITY CURVE + REGIME OVERLAY
# -------------------------------------------------------------------

st.subheader("Equity Curve with Regime Overlay")

eq_df = _portfolio_log()
reg_df = _regime_history()

if not eq_df.empty:
    fig = go.Figure()

    # Regime background bands
    if not reg_df.empty:
        merged = reg_df.merge(eq_df, on="date", how="inner")
        for label in merged["regime_label"].unique():
            mask = merged["regime_label"] == label
            fig.add_trace(go.Scatter(
                x=merged.loc[mask, "date"],
                y=merged.loc[mask, "total_nav"],
                fill="tozeroy",
                mode="none",
                name=label,
                fillcolor=REGIME_COLORS.get(label, "#9e9e9e"),
                opacity=0.15,
            ))

    # Equity line
    fig.add_trace(go.Scatter(
        x=eq_df["date"], y=eq_df["total_nav"],
        mode="lines", name="NAV", line=dict(color="#1565c0", width=2),
    ))
    fig.update_layout(
        xaxis_title="Date", yaxis_title="NAV ($)",
        margin=dict(t=10, b=10), hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No portfolio history yet — run the bot for at least one day.")

# -------------------------------------------------------------------
# REGIME HISTORY
# -------------------------------------------------------------------

if not reg_df.empty:
    st.subheader("Regime History")
    col_reg, col_conf = st.columns(2)
    with col_reg:
        fig_reg = px.scatter(reg_df, x="date", y="regime_label",
                             color="regime_label",
                             color_discrete_map=REGIME_COLORS,
                             title="Daily Regime Label")
        fig_reg.update_layout(showlegend=False, margin=dict(t=30, b=10))
        st.plotly_chart(fig_reg, use_container_width=True)
    with col_conf:
        fig_conf = px.line(reg_df, x="date", y="confidence", title="Regime Confidence")
        fig_conf.add_hline(y=0.55, line_dash="dash", line_color="red",
                           annotation_text="min trade threshold")
        fig_conf.update_layout(margin=dict(t=30, b=10))
        st.plotly_chart(fig_conf, use_container_width=True)

# -------------------------------------------------------------------
# POSITIONS
# -------------------------------------------------------------------

col_open, col_closed = st.columns(2)

with col_open:
    st.subheader("Open Positions")
    pos_df = _open_positions()
    if not pos_df.empty:
        st.dataframe(pos_df, use_container_width=True)
    else:
        st.info("No open positions.")

with col_closed:
    st.subheader("Recent Closed Trades")
    closed_df = _closed_positions()
    if not closed_df.empty:
        closed_df["realized_pnl"] = closed_df["realized_pnl"].round(2)
        st.dataframe(closed_df, use_container_width=True)
    else:
        st.info("No closed trades yet.")

# -------------------------------------------------------------------
# RISK CONTROLS
# -------------------------------------------------------------------

st.subheader("Risk Control Status")

rcol1, rcol2, rcol3, rcol4 = st.columns(4)
with rcol1:
    st.metric("Peak NAV", f"${risk.get('peak_nav', 0):,.0f}")
with rcol2:
    st.metric("Day Start NAV", f"${risk.get('day_start_nav', 0):,.0f}")
with rcol3:
    st.metric("Week Start NAV", f"${risk.get('week_start_nav', 0):,.0f}")
with rcol4:
    lock = risk.get("lock_file_exists", False)
    st.metric("Lock File", "🔒 ACTIVE" if lock else "✅ Clear")

risk_events_df = _risk_events()
if not risk_events_df.empty:
    st.dataframe(risk_events_df, use_container_width=True)

# -------------------------------------------------------------------
# FOOTER
# -------------------------------------------------------------------

st.divider()
st.caption(
    "⚠️ **Paper mode only.** Live order execution is intentionally disabled. "
    "All positions are simulated. This system is for research purposes."
)
