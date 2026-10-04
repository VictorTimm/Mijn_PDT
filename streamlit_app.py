"""CSV portfolio dashboard for DEGIRO, Bitvavo, and Ledger."""

from __future__ import annotations

import base64
import html
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from processing.calculations import Portfolio, build_portfolio
from processing.enrich import (
    apply_user_overrides,
    enrich_holdings,
    price_hints_with_details,
)
from processing.lookthrough import apply_lookthrough, load_etf_composition
from processing.normalize import (
    cache_exists,
    cache_meta,
    clear_cache,
    degiro_venue_balances,
    load_cache,
    parse_uploads,
    save_cache,
)

SAMPLE_DIR = Path(__file__).resolve().parent / "samples"

st.set_page_config(page_title="Portfolio", layout="wide", initial_sidebar_state="collapsed")

LABELS = {"degiro": "DEGIRO", "bitvavo": "Bitvavo", "ledger": "Ledger"}
BROKER_CHOICES = (
    ("all", "All brokers"),
    ("degiro", "DeGiro"),
    ("ledger", "Ledger"),
    ("bitvavo", "Bitvavo"),
)
PAGES = ("Portfolio", "Holdings", "Transactions", "DEGIRO")
PALETTE = [
    "#7EB6E6",
    "#5B8DEF",
    "#3D6FD8",
    "#2F6B4F",
    "#3EAA7A",
    "#7DCEA0",
    "#F0A07A",
    "#E07A5F",
    "#E8C36A",
    "#C45C4A",
    "#8E6BB5",
    "#6EC4C4",
]
GROUPS = {
    "Sector": "sector",
    "Country": "country",
    "Currency": "currency",
}
TYPE_LABELS = {
    "equity": "Equity",
    "etf": "ETF",
    "fund": "Fund",
    "index": "Index",
    "crypto": "Crypto",
    "Crypto": "Crypto",
    "live": "Live",
    "cache": "Cached quote",
    "csv": "Last trade",
    "cost": "Cost basis",
    "unpriced": "Unpriced",
}


def main() -> None:
    uploads, process, reprocess, use_sample, dark = _sidebar_import()
    _css(dark)
    transactions, meta, warnings = _resolve(uploads, process, reprocess, use_sample)
    page, broker = _view_from_query()
    _sidebar_controls(transactions)
    selected = _filter_broker(transactions, broker)
    hints, _hint_sources, hint_details = (
        price_hints_with_details(selected) if selected is not None and not selected.empty else ({}, {}, {})
    )
    book = _book(selected, hints, hint_details) if selected is not None and not selected.empty else None
    notices = list(warnings)
    if book and book["override_error"]:
        notices.append(str(book["override_error"]))
    if book:
        notices.extend(_portfolio_notes(book["portfolio"]))
    _top_bar(book["portfolio"] if book else None, page, broker, notices)
    if page == "Portfolio":
        _portfolio_page(book, selected, meta, broker)
        return
    if page == "Holdings":
        _holdings_page(book, broker)
        return
    if page == "DEGIRO":
        _degiro_page(selected, book, broker)
        return
    _transactions_page(selected, broker)


def _css(dark: bool) -> None:
    if dark:
        theme = """
          :root {
            --pdt-bg: #071c1b;
            --pdt-ink: #f4f7f6;
            --pdt-muted: #a7b9b4;
            --pdt-line: #1c3a36;
            --pdt-card: #0c2624;
            --pdt-pos: #3dce8a;
            --pdt-neg: #e07a5f;
          }
          [data-testid="stSidebar"], [data-testid="stSidebarContent"] {
            background: #061615; border-right: 1px solid var(--pdt-line);
          }
          [data-testid="stSidebar"] * { color: var(--pdt-ink); }
        """
    else:
        theme = """
          :root {
            --pdt-bg: #f6f7f4;
            --pdt-ink: #14221f;
            --pdt-muted: #4d5b57;
            --pdt-line: #e3e6e1;
            --pdt-card: #ffffff;
            --pdt-pos: #146b42;
            --pdt-neg: #c56047;
          }
          [data-testid="stSidebar"], [data-testid="stSidebarContent"] {
            background: #f0f2ee; border-right: 1px solid var(--pdt-line);
          }
          [data-testid="stSidebar"] label,
          [data-testid="stSidebar"] p,
          [data-testid="stSidebar"] span,
          [data-testid="stSidebar"] h1,
          [data-testid="stSidebar"] h2,
          [data-testid="stSidebar"] h3 { color: var(--pdt-ink); }
          [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
          [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color: var(--pdt-muted); }
          [data-testid="stFileUploaderDropzone"],
          [data-baseweb="input"] > div,
          [data-baseweb="textarea"],
          [data-baseweb="select"] > div,
          [data-testid="stExpander"] details {
            background: #ffffff !important;
            color: var(--pdt-ink) !important;
          }
          .pdt-stat-capital .pdt-stat-label { color: #2f5fbf; }
          .pdt-stat-total .pdt-stat-label { color: #1f7a7a; }
          .pdt-stat-gain .pdt-stat-label, .pdt-stat-gain .pdt-stat-value { color: #157a48; }
          .pdt-stat-loss .pdt-stat-label, .pdt-stat-loss .pdt-stat-value { color: #a84832; }
        """
    st.markdown(
        f"""
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;500;600&display=swap');
          [data-testid="stHeader"], [data-testid="stToolbar"] {{ display: none; }}
          .stApp, [data-testid="stAppViewContainer"] {{ background: var(--pdt-bg); color: var(--pdt-ink); }}
          html, body, [class*="css"] {{ font-family: "Source Sans 3", "Segoe UI", sans-serif; }}
          .block-container {{ padding-top: 0.6rem; max-width: 1280px; }}
          h1, h2, h3, .stCaption, [data-testid="stCaptionContainer"] {{ color: var(--pdt-ink); }}
          [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: var(--pdt-muted); }}
          .st-key-pdt_topbar {{
            background: var(--pdt-card); border: 1px solid var(--pdt-line); border-radius: 12px;
            padding: 0.4rem 0.7rem; margin-bottom: 1.05rem;
            position: sticky; top: 0.35rem; z-index: 100;
          }}
          .st-key-pdt_topbar,
          .st-key-pdt_topbar [data-testid="stVerticalBlock"],
          .st-key-pdt_topbar [data-testid="stColumn"],
          .st-key-pdt_topbar [data-testid="stElementContainer"] {{ overflow: visible !important; }}
          .st-key-pdt_topbar .stHtml, .st-key-pdt_topbar [data-testid="stHtml"] {{ overflow: visible !important; }}
          .st-key-pdt_topbar [data-testid="stElementContainer"],
          .st-key-pdt_topbar [data-testid="stMarkdownContainer"],
          .st-key-pdt_topbar [data-testid="stVerticalBlock"] {{ margin: 0 !important; padding: 0 !important; gap: 0 !important; }}
          .st-key-pdt_topbar [data-testid="stHorizontalBlock"] {{ align-items: center; gap: 0.55rem; }}
          .st-key-pdt_topbar [data-testid="stColumn"] > div {{ min-height: 0; }}
          .pdt-nav {{ display: flex; align-items: center; gap: 1.15rem; height: 36px; width: 100%; }}
          .pdt-actions {{ display: flex; align-items: center; justify-content: flex-end; gap: 0.45rem; }}
          a.pdt-eye {{
            width: 36px; height: 36px; box-sizing: border-box; border-radius: 10px; border: 1px solid var(--pdt-line);
            background: var(--pdt-card); color: var(--pdt-ink); display: inline-flex; align-items: center; justify-content: center;
            text-decoration: none; font-size: 1.05rem; line-height: 1; position: relative; flex: 0 0 auto;
          }}
          a.pdt-eye:hover {{ background: var(--pdt-bg); }}
          .pdt-eye.is-hidden::after {{
            content: ""; position: absolute; width: 18px; height: 1.5px; background: var(--pdt-ink); transform: rotate(-28deg);
          }}
          .pdt-bell {{ position: relative; }}
          .pdt-bell summary {{
            list-style: none; width: 36px; height: 36px; box-sizing: border-box; border-radius: 10px;
            border: 1px solid var(--pdt-line); background: var(--pdt-card); color: var(--pdt-ink); display: inline-flex;
            align-items: center; justify-content: center; cursor: pointer; position: relative;
          }}
          .pdt-bell summary::-webkit-details-marker {{ display: none; }}
          .pdt-bell summary:hover {{ background: var(--pdt-bg); }}
          .pdt-bell-dot {{
            position: absolute; top: 6px; right: 6px; width: 8px; height: 8px; border-radius: 999px; background: var(--pdt-neg);
          }}
          .pdt-bell-menu {{
            position: absolute; top: calc(100% + 0.35rem); right: 0; z-index: 50; width: 22rem;
            background: var(--pdt-card); color: var(--pdt-ink); border: 1px solid var(--pdt-line); border-radius: 10px;
            padding: 0.7rem 0.85rem; box-shadow: 0 10px 24px rgba(20, 34, 31, 0.12);
          }}
          .pdt-bell-menu ul {{ margin: 0; padding-left: 1.1rem; }}
          .pdt-bell-menu li {{ margin: 0.25rem 0; font-size: 0.92rem; line-height: 1.35; color: var(--pdt-ink); }}
          a.pdt-nav-link {{
            color: var(--pdt-muted); text-decoration: none; font-weight: 500; font-size: 1rem;
            padding: 0.2rem 0.05rem; border-bottom: 2px solid transparent; line-height: 1.2;
          }}
          a.pdt-nav-link.is-active {{ color: var(--pdt-ink); font-weight: 600; border-bottom-color: var(--pdt-ink); }}
          .pdt-nav-drop {{ position: relative; padding-bottom: 0.35rem; }}
          .pdt-nav-menu {{
            display: none; position: absolute; top: 100%; left: 0; z-index: 40;
            min-width: 11rem; background: var(--pdt-card); border: 1px solid var(--pdt-line); border-radius: 10px;
            padding: 0.3rem; box-shadow: 0 10px 24px rgba(20, 34, 31, 0.12);
          }}
          .pdt-nav-drop:hover .pdt-nav-menu, .pdt-nav-drop:focus-within .pdt-nav-menu {{ display: flex; flex-direction: column; }}
          .pdt-nav-menu a {{
            color: var(--pdt-ink); text-decoration: none; border-radius: 8px; padding: 0.42rem 0.65rem;
          }}
          .pdt-nav-menu a:hover {{ background: var(--pdt-bg); }}
          .pdt-nav-menu a.is-active {{ font-weight: 600; background: var(--pdt-bg); }}
          .st-key-pdt_topbar [data-testid="stColumn"]:last-child {{
            display: flex; justify-content: flex-end; align-items: center;
          }}
          .pdt-menu-btn {{
            width: 36px; height: 36px; box-sizing: border-box; border-radius: 10px; border: 1px solid var(--pdt-line);
            background: var(--pdt-card); color: var(--pdt-ink); display: inline-flex; align-items: center;
            justify-content: center; cursor: pointer; padding: 0; font-size: 1.05rem; line-height: 1;
          }}
          .pdt-menu-btn:hover {{ background: var(--pdt-bg); }}
          .pdt-gain-pill {{
            display: inline-flex; flex-direction: column; align-items: flex-end; justify-content: center;
            box-sizing: border-box; height: 36px; max-height: 36px; min-width: 8.2rem;
            padding: 0 0.7rem; border: 1px solid rgba(61, 206, 138, 0.45);
            border-radius: 10px; background: rgba(61, 206, 138, 0.16); text-align: right;
            font-variant-numeric: tabular-nums; line-height: 1.05; overflow: hidden;
          }}
          .pdt-gain-pill.is-loss {{ background: rgba(224, 122, 95, 0.16); border-color: rgba(224, 122, 95, 0.5); }}
          .pdt-topmetric-main {{ font-size: 1rem; font-weight: 600; color: var(--pdt-ink); }}
          .pdt-topmetric-sub {{ font-size: 0.75rem; color: var(--pdt-muted); margin-top: 0.08rem; }}
          .pdt-pos {{ color: var(--pdt-pos); }}
          .pdt-neg {{ color: var(--pdt-neg); }}
          .pdt-kicker {{
            font-size: 2.1rem; font-weight: 500; letter-spacing: -0.03em;
            color: var(--pdt-ink); line-height: 1.08; margin: 0.25rem 0 0.3rem;
          }}
          .pdt-chart-title {{
            color: var(--pdt-ink); font-size: 1rem; font-weight: 600;
            margin: 0.35rem 0 0.15rem;
          }}
          .pdt-stats {{ display: flex; flex-wrap: wrap; gap: 0.55rem; margin: 0.15rem 0 0.85rem; }}
          .pdt-stat {{
            display: inline-flex; align-items: baseline; gap: 0.55rem;
            border-radius: 999px; padding: 0.42rem 0.85rem; border: 1px solid transparent;
            font-variant-numeric: tabular-nums;
          }}
          .pdt-stat-label {{ font-size: 0.78rem; letter-spacing: 0.01em; font-weight: 600; }}
          .pdt-stat-value {{ font-size: 0.98rem; font-weight: 600; color: var(--pdt-ink); }}
          .pdt-stat-capital {{ background: rgba(91, 141, 239, 0.16); border-color: rgba(91, 141, 239, 0.45); }}
          .pdt-stat-capital .pdt-stat-label {{ color: #9ebdf2; }}
          .pdt-stat-total {{ background: rgba(110, 196, 196, 0.16); border-color: rgba(110, 196, 196, 0.45); }}
          .pdt-stat-total .pdt-stat-label {{ color: #b7e4e4; }}
          .pdt-stat-gain {{ background: rgba(61, 206, 138, 0.16); border-color: rgba(61, 206, 138, 0.5); }}
          .pdt-stat-gain .pdt-stat-label, .pdt-stat-gain .pdt-stat-value {{ color: #8ee0b6; }}
          .pdt-stat-loss {{ background: rgba(224, 122, 95, 0.16); border-color: rgba(224, 122, 95, 0.5); }}
          .pdt-stat-loss .pdt-stat-label, .pdt-stat-loss .pdt-stat-value {{ color: #f0b5a4; }}
          [data-testid="stPopover"] > button {{
            background: transparent; color: var(--pdt-muted);
            border: none; border-radius: 6px;
            font-weight: 500; font-size: 0.82rem; margin-bottom: 0.45rem;
          }}
          .pdt-header-actions {{ display: flex; gap: 0.45rem; justify-content: flex-end; }}
          .pdt-pill {{ display: inline-flex; align-items: center; border: 1px solid var(--pdt-line); border-radius: 999px; padding: 0.26rem 0.68rem; font-size: 0.86rem; color: var(--pdt-ink); background: rgba(255,255,255,0.02); }}
          .pdt-pill-dot {{ width: 8px; height: 8px; border-radius: 999px; background: var(--pdt-muted); margin-right: 0.38rem; }}

          [data-testid="stRadio"] div[role="radiogroup"] {{ gap: 0.95rem; }}
          [data-testid="stRadio"] label[data-baseweb="radio"] {{ margin-right: 0.3rem; }}
          [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {{ display: none; }}
          [data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child {{
            color: var(--pdt-muted); font-weight: 500; border-bottom: 2px solid transparent; padding-bottom: 0.3rem;
          }}
          [data-testid="stRadio"] label[data-baseweb="radio"][aria-checked="true"] > div:last-child {{
            color: var(--pdt-ink); border-bottom-color: var(--pdt-ink);
          }}
          [data-testid="stRadio"] span {{ font-size: 1rem; }}

          [data-testid="stSegmentedControl"] button {{
            background: transparent !important; border: none !important;
            border-radius: 0 !important; color: var(--pdt-muted) !important;
            box-shadow: none !important; padding-left: 0 !important; padding-right: 0.7rem !important;
          }}
          [data-testid="stSegmentedControl"] button[aria-checked="true"] {{
            color: var(--pdt-ink) !important;
            border-bottom: 2px solid var(--pdt-ink) !important;
          }}
          .stButton > button {{
            border-radius: 999px; border: 1px solid var(--pdt-line);
            background: var(--pdt-card); color: var(--pdt-ink); font-weight: 500;
          }}
          .stButton > button[kind="primary"] {{ background: #3dce8a; color: #062018; border-color: transparent; }}
          .st-key-pdt_topbar [data-testid="stBaseButton-segmented_control"],
          .st-key-pdt_topbar [data-testid="stBaseButton-segmented_controlActive"] {{
            background: transparent !important;
            color: var(--pdt-muted) !important;
            border: none !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            border-bottom: 2px solid transparent !important;
          }}
          .st-key-pdt_topbar [data-testid="stBaseButton-segmented_controlActive"] {{
            color: var(--pdt-ink) !important;
            font-weight: 600;
            border-bottom: 2px solid var(--pdt-ink) !important;
          }}
          [data-testid="stAlert"] p, [data-testid="stAlert"] div {{
            color: var(--pdt-ink) !important;
          }}
          .pdt-legend-wrap {{
            border: 1px solid var(--pdt-line); border-radius: 16px; padding: 1rem 1rem 0.9rem; position: relative; overflow: hidden;
            background:
              linear-gradient(150deg, rgba(255,255,255,0.05) 1px, transparent 1px) 0 0 / 110px 90px,
              linear-gradient(30deg, rgba(255,255,255,0.04) 1px, transparent 1px) 0 0 / 110px 90px;
          }}
          .pdt-legend {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.2rem 2.5rem; }}
          .pdt-row {{ display: grid; grid-template-columns: 14px minmax(0, 1.6fr) minmax(40px, 22%) 4.6rem; gap: 0.55rem; align-items: center; min-height: 1.85rem; }}
          .pdt-swatch {{ width: 12px; height: 12px; border-radius: 4px; }}
          .pdt-name {{ color: var(--pdt-ink); font-size: 0.96rem; white-space: normal; overflow: visible; line-height: 1.25; }}
          .pdt-track {{ height: 3px; background: transparent; position: relative; }}
          .pdt-bar {{ display: block; height: 3px; border-radius: 2px; }}
          .pdt-pct {{ text-align: right; font-variant-numeric: tabular-nums; color: var(--pdt-ink); font-size: 0.95rem; }}
          .stDataFrame, [data-testid="stDataFrame"] {{ border: 1px solid var(--pdt-line); border-radius: 12px; }}
          .stDownloadButton button {{ border-radius: 999px; }}
          {theme}
          [data-testid="stPopover"] button {{
            background: transparent !important;
            color: var(--pdt-muted) !important;
            border: none !important;
            border-radius: 6px !important;
            box-shadow: none !important;
            font-size: 0.82rem !important;
            font-weight: 500 !important;
            padding: 0.1rem 0.2rem !important;
            min-height: 0 !important;
          }}
          [data-testid="stWidgetLabel"] p,
          [data-testid="stWidgetLabel"] span,
          [data-testid="stCheckbox"] p,
          [data-testid="stCheckbox"] span {{
            color: var(--pdt-ink) !important;
          }}
          [data-testid="stBaseButton-segmented_control"],
          [data-testid="stBaseButton-segmented_controlActive"] {{
            background: var(--pdt-card) !important;
            color: var(--pdt-muted) !important;
            border: 1px solid var(--pdt-line) !important;
          }}
          [data-testid="stBaseButton-segmented_controlActive"] {{
            color: var(--pdt-ink) !important;
            background: var(--pdt-bg) !important;
            border-bottom: 2px solid var(--pdt-ink) !important;
          }}
          [data-testid="stFileUploaderDropzone"],
          [data-testid="stFileUploaderDropzone"] button,
          [data-testid="stFileUploaderDropzone"] span,
          [data-testid="stFileUploaderDropzone"] small {{
            background: var(--pdt-card) !important;
            color: var(--pdt-ink) !important;
          }}
          [data-testid="stExpander"] details,
          [data-testid="stExpander"] summary,
          [data-testid="stExpander"] summary span {{
            background: var(--pdt-card) !important;
            color: var(--pdt-ink) !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


_UPLOAD_HTML = """
<div style="font-family: inherit; color: inherit;">
  <label style="display:block; font-size:14px; margin-bottom:6px;">CSV exports</label>
  <input id="files" type="file" accept=".csv,text/csv" multiple
    style="width:100%; font-size:13px;" />
  <div id="names" style="font-size:12px; margin-top:6px; opacity:0.8;"></div>
</div>
<script>
function send(type, data) {
  window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, data), "*");
}
function ready() {
  send("streamlit:componentReady", {apiVersion: 1});
  send("streamlit:setFrameHeight", {height: 78});
}
function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}
document.getElementById("files").addEventListener("change", async (event) => {
  const chosen = Array.from(event.target.files || []);
  document.getElementById("names").textContent = chosen.map((file) => file.name).join(", ");
  const payload = [];
  for (const file of chosen) {
    payload.push({name: file.name, data: toBase64(await file.arrayBuffer())});
  }
  send("streamlit:setComponentValue", {value: payload, dataType: "json"});
});
window.addEventListener("message", (event) => {
  if (event.data && event.data.type === "streamlit:render") ready();
});
ready();
</script>
"""


class _CsvUpload:
    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def _csv_uploads() -> list[_CsvUpload]:
    picked = components.html(_UPLOAD_HTML, height=86)
    if picked:
        files = []
        for item in picked:
            raw = item.get("data") if isinstance(item, dict) else None
            name = str(item.get("name") or "export.csv") if isinstance(item, dict) else "export.csv"
            if not raw:
                continue
            files.append(_CsvUpload(name, base64.b64decode(raw)))
        if files:
            st.session_state["csv_uploads"] = files
    stored = st.session_state.get("csv_uploads") or []
    return list(stored)


def _sidebar_import() -> tuple[list, bool, bool, bool, bool]:
    with st.sidebar:
        st.header("Import")
        st.caption("DEGIRO, Bitvavo, and Ledger. Files stay on this computer.")
        uploads = _csv_uploads()
        if uploads:
            st.caption(", ".join(item.name for item in uploads))
        process = st.button("Process", type="primary", disabled=not uploads, width="stretch")
        reprocess = st.button("Re-process", disabled=not uploads, width="stretch")
        use_sample = st.button("Use sample data", width="stretch")
        st.caption("Sample files live in the samples folder. Edit overrides.json to set a sector or country.")
        dark = st.toggle("Dark mode", value=bool(st.session_state.get("dark", True)))
        st.session_state["dark"] = dark
    return uploads or [], process, reprocess, use_sample, dark


def _sidebar_controls(transactions: pd.DataFrame | None) -> None:
    with st.sidebar:
        st.divider()
        if st.button("Clear cache", disabled=not cache_exists(), width="stretch"):
            clear_cache()
            st.session_state.pop("warnings", None)
            st.rerun()
        if transactions is not None and transactions.empty:
            st.caption("No cached data available yet.")


def _view_from_query() -> tuple[str, str]:
    page = str(st.query_params.get("page", "Portfolio"))
    broker = str(st.query_params.get("broker", "all"))
    if page not in PAGES:
        page = "Portfolio"
    known = {key for key, _label in BROKER_CHOICES}
    if broker not in known:
        broker = "all"
    if page == "DEGIRO" and broker not in {"all", "degiro"}:
        page = "Portfolio"
    return page, broker


def _filter_broker(transactions: pd.DataFrame | None, broker: str) -> pd.DataFrame | None:
    if transactions is None or transactions.empty or broker == "all":
        return transactions
    return transactions.loc[transactions["broker"].eq(broker)].reset_index(drop=True)


def _menu_button() -> None:
    st.html(
        """
        <button type="button" class="pdt-menu-btn" aria-label="Open menu" title="Open menu">&#9776;</button>
        <script>
          const button = document.querySelector(".pdt-menu-btn");
          if (button) {
            button.onclick = () => {
              const collapse = document.querySelector('[data-testid="stSidebarCollapseButton"] button');
              const expand = document.querySelector('[data-testid="stExpandSidebarButton"]');
              if (collapse) collapse.click();
              else if (expand) expand.click();
            };
          }
        </script>
        """,
        unsafe_allow_javascript=True,
        width="content",
    )


def _hidden_money() -> bool:
    return str(st.query_params.get("privacy", "0")) == "1"


def _query(page: str, broker: str, privacy: bool | None = None) -> str:
    hidden = _hidden_money() if privacy is None else privacy
    return f"?page={page}&broker={broker}&privacy={1 if hidden else 0}"


def _nav_html(page: str, broker: str) -> str:
    items = []
    for name in PAGES:
        if name == "DEGIRO" and broker not in {"all", "degiro"}:
            continue
        active = " is-active" if name == page else ""
        href = _query(name, broker)
        nav_label = "Costs" if name == "DEGIRO" else name
        if name == "Portfolio":
            links = []
            for key, label in BROKER_CHOICES:
                chosen = " is-active" if key == broker else ""
                links.append(f"<a class='{chosen.strip()}' href='{_query('Portfolio', key)}'>{label}</a>")
            items.append(
                "<span class='pdt-nav-drop'>"
                f"<a class='pdt-nav-link{active}' href='{href}'>Portfolio</a>"
                f"<span class='pdt-nav-menu'>{''.join(links)}</span>"
                "</span>"
            )
        else:
            items.append(f"<a class='pdt-nav-link{active}' href='{href}'>{nav_label}</a>")
    return f"<nav class='pdt-nav'>{''.join(items)}</nav>"


def _eye_link(page: str, broker: str) -> str:
    hidden = _hidden_money()
    eye = " is-hidden" if hidden else ""
    label = "Show amounts" if hidden else "Hide amounts"
    return f"<a class='pdt-eye{eye}' href='{_query(page, broker, not hidden)}' title='{label}' aria-label='{label}'>&#128065;</a>"


def _bell(notices: list[str]) -> str:
    if not notices:
        return ""
    items = "".join(f"<li>{html.escape(message)}</li>" for message in notices)
    return (
        "<details class='pdt-bell'>"
        "<summary aria-label='Notices' title='Notices'>&#128276;<span class='pdt-bell-dot'></span></summary>"
        f"<div class='pdt-bell-menu'><ul>{items}</ul></div>"
        "</details>"
    )


def _top_bar(portfolio: Portfolio | None, page: str, broker: str, notices: list[str] | None = None) -> None:
    with st.container(key="pdt_topbar"):
        menu, nav, metric = st.columns([0.28, 2.3, 1.15], vertical_alignment="center")
        with menu:
            _menu_button()
        with nav:
            st.html(_nav_html(page, broker), width="stretch")
        with metric:
            pill_class = "pdt-gain-pill"
            if portfolio is None:
                body = "<div class='pdt-topmetric-main'>--</div><div class='pdt-topmetric-sub'>No data loaded</div>"
            else:
                capital, total, result_value = _scoped_totals(portfolio)
                ratio = result_value / capital * 100 if abs(capital) > 0 else 0.0
                sign_class = "pdt-pos" if result_value >= 0 else "pdt-neg"
                if result_value < 0:
                    pill_class += " is-loss"
                label = "Total gain" if result_value >= 0 else "Total loss"
                if _hidden_money():
                    body = (
                        f"<div class='pdt-topmetric-main {sign_class}'>{_pct(ratio, 2)}</div>"
                        f"<div class='pdt-topmetric-sub'>{label}</div>"
                    )
                else:
                    body = (
                        f"<div class='pdt-topmetric-main {sign_class}'>{_eur(result_value)}</div>"
                        f"<div class='pdt-topmetric-sub'>{label} {_pct(ratio, 2)}</div>"
                    )
            st.markdown(
                f"<div class='pdt-actions'>{_bell(notices or [])}{_eye_link(page, broker)}<div class='{pill_class}'>{body}</div></div>",
                unsafe_allow_html=True,
            )


def _portfolio_page(book: dict[str, object] | None, selected: pd.DataFrame | None, meta: dict | None, broker: str) -> None:
    if meta:
        _data_menu(meta)
    if selected is None or selected.empty or book is None:
        _empty_state()
        return
    if broker != "all":
        st.caption(f"Showing {dict(BROKER_CHOICES).get(broker, broker)} only.")
    portfolio = book["portfolio"]
    assert isinstance(portfolio, Portfolio)
    _summary_line(portfolio)
    _value_history(portfolio, selected)
    _allocation(portfolio.holdings, broker)


def _holdings_page(book: dict[str, object] | None, broker: str) -> None:
    st.markdown('<div class="pdt-kicker">Holdings</div>', unsafe_allow_html=True)
    if broker != "all":
        st.caption(f"Showing {dict(BROKER_CHOICES).get(broker, broker)} only.")
    if book is None:
        st.info("No open positions. Add CSV exports and process data.")
        return
    portfolio = book["portfolio"]
    assert isinstance(portfolio, Portfolio)
    _holdings(portfolio.holdings)


def _transactions_page(selected: pd.DataFrame | None, broker: str) -> None:
    st.markdown('<div class="pdt-kicker">Transactions</div>', unsafe_allow_html=True)
    if broker != "all":
        st.caption(f"Showing {dict(BROKER_CHOICES).get(broker, broker)} only.")
    if selected is None or selected.empty:
        st.info("No transactions available. Add CSV exports and process data.")
        return
    _transactions(selected)


def _resolve(uploads, process: bool, reprocess: bool, use_sample: bool) -> tuple[pd.DataFrame | None, dict | None, list[str]]:
    if use_sample:
        payload = _sample_payload()
        if not payload:
            return None, None, ["The samples folder has no CSV files."]
        return _store(payload, force=True)
    if (process or reprocess) and uploads:
        payload = tuple((file.name, file.getvalue()) for file in uploads)
        return _store(payload, force=reprocess)
    transactions, meta = load_cache()
    return transactions, meta, list(st.session_state.get("warnings") or [])


def _sample_payload() -> tuple[tuple[str, bytes], ...]:
    if not SAMPLE_DIR.exists():
        return ()
    return tuple((path.name, path.read_bytes()) for path in sorted(SAMPLE_DIR.glob("*.csv")))


def _store(payload: tuple[tuple[str, bytes], ...], force: bool) -> tuple[pd.DataFrame | None, dict | None, list[str]]:
    if force:
        _parse_cached.clear()
    with st.spinner("Reading exports"):
        transactions, counts, warnings = _parse_cached(payload)
    st.session_state["warnings"] = warnings
    if transactions is not None and not transactions.empty:
        meta = cache_meta(counts)
        save_cache(transactions, meta)
        return transactions, meta, warnings
    cached, meta = load_cache()
    if cached is not None and not cached.empty:
        warnings = [*warnings, "Showing the last successful import."]
    return cached, meta, warnings


@st.cache_data(show_spinner=False)
def _parse_cached(payload: tuple[tuple[str, bytes], ...]):
    result = parse_uploads(payload)
    return result.transactions, result.counts, result.warnings


def _book(
    rows: pd.DataFrame,
    hints: dict[str, float],
    hint_details: dict[str, dict[str, str]],
) -> dict[str, object]:
    portfolio = build_portfolio(rows, hints, hint_details)
    portfolio.holdings = enrich_holdings(portfolio.holdings)
    portfolio.holdings, override_error = apply_user_overrides(portfolio.holdings)
    source_counts, holding_sources = _price_sources(portfolio)
    portfolio.holdings = _attach_price_source(portfolio.holdings, holding_sources, portfolio.positions)
    portfolio.holdings = _mark_etf_rollups(portfolio.holdings)
    priced_count = source_counts["live"] + source_counts["cache"] + source_counts["csv"]
    return {
        "portfolio": portfolio,
        "source_counts": source_counts,
        "priced_count": priced_count,
        "override_error": override_error,
    }


def _summary_line(portfolio: Portfolio) -> None:
    capital, total, result_value = _scoped_totals(portfolio)
    ratio = result_value / capital * 100 if abs(capital) > 0 else 0.0
    result = "pdt-stat-gain" if result_value >= 0 else "pdt-stat-loss"
    result_text = _pct(ratio, 2) if _hidden_money() else f"{_eur(result_value)} · {_pct(ratio, 2)}"
    st.markdown(
        "<div class='pdt-stats'>"
        f"<div class='pdt-stat pdt-stat-capital'><span class='pdt-stat-label'>Capital invested</span><span class='pdt-stat-value'>{_eur(capital)}</span></div>"
        f"<div class='pdt-stat {result}'><span class='pdt-stat-label'>Result</span><span class='pdt-stat-value'>{result_text}</span></div>"
        f"<div class='pdt-stat pdt-stat-total'><span class='pdt-stat-label'>Total</span><span class='pdt-stat-value'>{_eur(total)}</span></div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _portfolio_notes(portfolio: Portfolio) -> list[str]:
    notes = []
    if portfolio.other_cash:
        others = ", ".join(
            f"{code} {'***' if _hidden_money() else _fmt_number(amount, 2)}"
            for code, amount in sorted(portfolio.other_cash.items())
        )
        notes.append(f"Cash in other currencies: {others}")
    if not portfolio.positions.empty and bool(portfolio.positions["valued_at_cost"].any()):
        notes.append("Positions without a euro price in the files are valued at cost.")
    if not portfolio.positions.empty and "unpriced" in portfolio.positions.columns and bool(portfolio.positions["unpriced"].any()):
        notes.append("Unpriced positions are shown separately and excluded from allocation weights.")
    return notes


def _buy_markers(transactions: pd.DataFrame | None, history: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "value", "kind", "hover"]
    if transactions is None or transactions.empty or "type" not in transactions.columns:
        return pd.DataFrame(columns=columns)
    buys = transactions.loc[transactions["type"].astype(str).str.lower().eq("buy")].copy()
    if buys.empty:
        return pd.DataFrame(columns=columns)
    buys["date"] = pd.to_datetime(buys["date"], errors="coerce").dt.normalize()
    buys = buys.dropna(subset=["date"])
    if buys.empty:
        return pd.DataFrame(columns=columns)
    broker = buys["broker"].astype(str).str.lower() if "broker" in buys.columns else pd.Series("", index=buys.index)
    buys["kind"] = "ETF"
    buys.loc[broker.isin(["bitvavo", "ledger"]), "kind"] = "Crypto"
    line = history.loc[:, ["date", "Crypto invested", "ETF invested"]].sort_values("date")
    line["date"] = pd.to_datetime(line["date"]).dt.normalize()
    placed = pd.merge_asof(buys.sort_values("date"), line, on="date", direction="nearest")
    rows = []
    for (day, kind), group in placed.groupby(["date", "kind"], sort=True):
        column = "Crypto invested" if kind == "Crypto" else "ETF invested"
        value = pd.to_numeric(group[column], errors="coerce").dropna()
        if value.empty:
            continue
        lines = [_buy_line(row) for row in group.itertuples(index=False)]
        rows.append({"date": day, "value": float(value.iloc[0]), "kind": kind, "hover": "<br>".join(lines)})
    return pd.DataFrame(rows, columns=columns)


def _buy_line(row: object) -> str:
    symbol = str(getattr(row, "symbol", "") or "").strip()
    name = str(getattr(row, "name", "") or "").strip()
    label = name if name and name.lower() not in {"nan", "none"} and name.upper() != symbol.upper() else symbol or name or "Buy"
    quantity = pd.to_numeric(getattr(row, "quantity", None), errors="coerce")
    amount = pd.to_numeric(getattr(row, "amount", None), errors="coerce")
    price = pd.to_numeric(getattr(row, "price", None), errors="coerce")
    if pd.isna(amount) and pd.notna(price) and pd.notna(quantity):
        amount = float(price) * float(quantity)
    spent = abs(float(amount)) if pd.notna(amount) else None
    qty = "***" if _hidden_money() or pd.isna(quantity) else _fmt_number(abs(float(quantity)), 4).rstrip("0").rstrip(",")
    cost = _eur(spent) if spent is not None else "—"
    return f"{label} · {qty} · {cost}"


def _value_history(portfolio: Portfolio, transactions: pd.DataFrame | None = None) -> None:
    history = portfolio.history.copy()
    if history.empty:
        return
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["Holdings value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["Deposits"] = pd.to_numeric(frame["deposits"], errors="coerce")
    frame["Combined invested"] = pd.to_numeric(frame.get("invested"), errors="coerce")
    frame["Crypto invested"] = pd.to_numeric(frame.get("crypto_invested"), errors="coerce")
    frame["ETF invested"] = pd.to_numeric(frame.get("etf_invested"), errors="coerce")
    frame = frame.dropna(subset=["date"])
    if frame.empty:
        return
    deposits = frame["Deposits"].fillna(0.0)
    invested = frame["Combined invested"].fillna(0.0)
    latest_deposits = float(deposits.iloc[-1]) if not deposits.empty else 0.0
    latest_invested = float(invested.iloc[-1]) if not invested.empty else 0.0
    missing_deposits = bool(deposits.abs().sum() <= 1e-9)
    incomplete_deposits = latest_invested > 0 and latest_deposits < latest_invested * 0.8
    use_capital = missing_deposits or incomplete_deposits
    series = ["Holdings value", "Combined invested", "Crypto invested", "ETF invested"]
    colors = {
        "Holdings value": PALETTE[1],
        "Combined invested": PALETTE[8],
        "Crypto invested": "#E8C36A",
        "ETF invested": "#6EC4C4",
    }
    points = frame.loc[:, ["date", *series]].melt(id_vars="date", var_name="Series", value_name="Amount")
    points["hover"] = points["Amount"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—")
    figure = px.line(points, x="date", y="Amount", color="Series", color_discrete_map=colors)
    for trace in figure.data:
        rows = points.loc[points["Series"].eq(trace.name)]
        trace.update(
            customdata=rows["hover"],
            hovertemplate="%{fullData.name}<br>%{x|%Y-%m-%d}<br>%{customdata}<extra></extra>",
            legendgroup=trace.name,
        )
    markers = _buy_markers(transactions, frame)
    for kind in ("Crypto", "ETF"):
        group = markers.loc[markers["kind"].eq(kind)] if not markers.empty else markers
        if group.empty:
            continue
        figure.add_scatter(
            x=group["date"],
            y=group["value"],
            mode="markers",
            name=kind,
            legendgroup=f"{kind} invested",
            showlegend=False,
            marker={"symbol": "diamond", "size": 9, "color": colors[f"{kind} invested"], "line": {"width": 1, "color": "#14221F"}},
            text=group["hover"],
            hovertemplate="%{x|%Y-%m-%d}<br>%{text}<extra></extra>",
        )
    _style(figure, "", height=360)
    figure.update_layout(
        margin={"l": 8, "r": 8, "t": 28, "b": 8},
        title={"text": ""},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1, "x": 0, "title": {"text": ""}},
    )
    st.markdown('<div class="pdt-chart-title">Value over time</div>', unsafe_allow_html=True)
    st.plotly_chart(figure, width="stretch", key="portfolio-value-history")
    note = "History uses the last euro trade price between transactions; the final point uses today's quote set."
    if use_capital and portfolio.invested > 0:
        note += " Deposits are missing for this broker view, so the chart shows capital invested instead."
    if use_capital and invested.abs().sum() <= 1e-9 and portfolio.market_value > 0:
        note += " This broker has holdings but no deposit or invested cash rows in the imported files."
    st.caption(note)


def _allocation(holdings: pd.DataFrame, broker: str = "all") -> None:
    valued = holdings.loc[pd.to_numeric(holdings["current_value"], errors="coerce").fillna(0).gt(0)].copy()
    if valued.empty:
        st.info("No open positions to break down.")
        return
    by_coin = broker in {"bitvavo", "ledger"}
    column = "symbol"
    composition: dict = {}
    group_col, crypto_col = st.columns([3.2, 1], vertical_alignment="bottom")
    with crypto_col:
        if "include_crypto" not in st.session_state:
            st.session_state["include_crypto"] = True
        include_crypto = st.toggle("Include crypto", key="include_crypto")
    if not include_crypto:
        valued = valued.loc[~_crypto_mask(valued)].copy()
    if valued.empty:
        st.info("No positions left to chart. Turn Include crypto back on to show those holdings.")
        return
    if by_coin:
        with group_col:
            st.caption("Allocation by cryptocurrency")
        valued["Group"] = valued["symbol"].map(_coin_label)
    else:
        with group_col:
            choice = st.segmented_control("Group by", list(GROUPS), default="Sector", key="group_by")
        column = GROUPS[choice or "Sector"]
        composition = load_etf_composition()
        if column in {"sector", "country", "currency"}:
            valued = apply_lookthrough(valued, column, composition)
        if not include_crypto:
            valued = valued.loc[~_crypto_mask(valued)].copy()
        valued["Group"] = valued[column].map(_group_label)
    valued["Holding"] = valued.apply(_holding_name, axis=1)
    valued["Value"] = pd.to_numeric(valued["current_value"], errors="coerce").fillna(0.0)
    summary = (
        valued.groupby("Group", as_index=False)["Value"]
        .sum()
        .sort_values("Value", ascending=False, kind="mergesort")
    )
    if not include_crypto:
        summary = summary.loc[~summary["Group"].astype(str).str.strip().str.lower().eq("crypto")].copy()
    total = float(summary["Value"].sum())
    summary["Share"] = summary["Value"] / total * 100 if total else 0.0
    if summary.empty:
        st.info("No positions left to chart. Turn Include crypto back on to show those holdings.")
        return
    if not by_coin and column == "sector" and not composition:
        st.caption("ETF sector look-through cache is empty. Run `python scripts/refresh_etf_composition.py` to load sector weights.")

    chart, legend = st.columns([1.05, 1.35])
    chart_key = f"alloc-{broker}-{int(include_crypto)}-{column}-{len(summary)}"
    with chart:
        st.plotly_chart(_pie(summary), width="stretch", key=chart_key)
    with legend:
        st.markdown(f"<div class='pdt-legend-wrap'>{_legend_html(summary)}</div>", unsafe_allow_html=True)
    with st.expander("Positions"):
        st.plotly_chart(_treemap(valued), width="stretch", key=f"{chart_key}-tree")


def _legend_html(summary: pd.DataFrame) -> str:
    rows = list(summary.itertuples(index=False))
    midpoint = (len(rows) + 1) // 2
    cells = []
    for index, row in enumerate(rows):
        color = PALETTE[index % len(PALETTE)]
        share = float(row.Share)
        width = max(share, 1.2)
        label = (
            str(row.Group)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        cells.append(
            "<div class='pdt-row'>"
            f"<span class='pdt-swatch' style='background:{color}'></span>"
            f"<span class='pdt-name'>{label}</span>"
            f"<span class='pdt-track'><span class='pdt-bar' style='width:{width:.1f}%;background:{color}'></span></span>"
            f"<span class='pdt-pct'>{_pct(share, 2)}</span>"
            "</div>"
        )
    left = "".join(cells[:midpoint])
    right = "".join(cells[midpoint:])
    return f"<div class='pdt-legend'><div>{left}</div><div>{right}</div></div>"


def _pie(summary: pd.DataFrame):
    chart_data = summary.copy()
    hover = [
        f"{group}<br>{_eur(float(value))}<br>{_pct(float(share), 2)}"
        for group, value, share in chart_data[["Group", "Value", "Share"]].itertuples(index=False)
    ]
    figure = px.pie(
        chart_data,
        names="Group",
        values="Value",
        hole=0.62,
        color="Group",
        color_discrete_sequence=PALETTE,
    )
    figure.update_traces(
        textinfo="none",
        customdata=hover,
        hovertemplate="%{customdata}<extra></extra>",
        sort=False,
        marker={"line": {"color": "rgba(0,0,0,0)", "width": 2}},
    )
    _style(figure, "", height=460)
    figure.update_layout(showlegend=False, margin={"l": 8, "r": 8, "t": 12, "b": 8})
    return figure


def _treemap(valued: pd.DataFrame):
    chart_data = valued.copy()
    total = float(pd.to_numeric(chart_data["Value"], errors="coerce").fillna(0).sum())
    details = []
    for group, holding, value in chart_data[["Group", "Holding", "Value"]].itertuples(index=False):
        share = float(value) / total * 100 if total else 0.0
        money = _eur(float(value))
        percent = _pct(share, 2)
        details.append([money, percent, f"{holding}<br>{group}<br>{money}<br>{percent}"])
    figure = px.treemap(
        chart_data,
        path=["Group", "Holding"],
        values="Value",
        color="Group",
        color_discrete_sequence=PALETTE,
    )
    figure.update_traces(
        texttemplate="%{label}<br>%{customdata[0]}<br>%{customdata[1]}",
        customdata=details,
        hovertemplate="%{customdata[2]}<extra></extra>",
    )
    figure.update_layout(margin={"l": 8, "r": 8, "t": 28, "b": 8})
    _style(figure, "Positions", height=420)
    return figure


def _holdings(holdings: pd.DataFrame) -> None:
    if holdings.empty:
        st.info("No open positions.")
        return
    search, kinds, sectors = st.columns([2, 1, 1])
    query = search.text_input("Search", placeholder="Symbol or name")
    type_options = sorted({_group_label(value) for value in holdings["security_type"]})
    sector_options = sorted({_group_label(value) for value in holdings["sector"]})
    chosen_types = kinds.multiselect("Security type", type_options, default=type_options)
    chosen_sectors = sectors.multiselect("Sector", sector_options, default=sector_options)
    view = holdings.copy()
    view["Security type"] = view["security_type"].map(_group_label)
    view["Sector"] = view["sector"].map(_group_label)
    if query:
        needle = query.strip().lower()
        view = view.loc[
            view["symbol"].str.lower().str.contains(needle, na=False)
            | view["name"].str.lower().str.contains(needle, na=False)
            | view["isin"].str.lower().str.contains(needle, na=False)
        ]
    view = view.loc[view["Security type"].isin(chosen_types) & view["Sector"].isin(chosen_sectors)]
    table = pd.DataFrame(
        {
            "Symbol": view["symbol"],
            "Name": view["name"],
            "Quantity": view["quantity"].map(_qty),
            "Avg cost": view["avg_cost"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—"),
            "Price": view.apply(_holding_price_label, axis=1),
            "Value": view.apply(_holding_value_label, axis=1),
            "Currency": view["currency"],
            "Sector": view["Sector"],
            "Industry": view["industry"].map(_group_label),
            "Country": view["country"].map(_group_label),
            "Exchange": view["exchange"].map(_group_label),
            "Security type": view["Security type"],
            "Price source": view.apply(_holding_price_source_label, axis=1),
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")


def _transactions(transactions: pd.DataFrame) -> None:
    kinds = list(dict.fromkeys(transactions["type"].tolist()))
    search, kind_filter = st.columns([2, 1])
    query = search.text_input("Search transactions", placeholder="Symbol, name, or ISIN")
    selected = kind_filter.multiselect("Type", kinds, default=kinds)
    view = transactions.loc[transactions["type"].isin(selected)].sort_values("date", ascending=False)
    if query:
        needle = query.strip().lower()
        view = view.loc[
            view["symbol"].str.lower().str.contains(needle, na=False)
            | view["name"].str.lower().str.contains(needle, na=False)
            | view["isin"].str.lower().str.contains(needle, na=False)
        ]
    table = pd.DataFrame(
        {
            "Date": pd.to_datetime(view["date"]).dt.strftime("%Y-%m-%d %H:%M"),
            "Broker": view["broker"].map(lambda source: LABELS.get(source, source)),
            "Type": view["type"],
            "Symbol": view["symbol"],
            "Name": view["name"],
            "ISIN": view["isin"],
            "Quantity": view["quantity"].map(_qty),
            "Price": view["price"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—"),
            "Amount": view["amount"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—"),
            "Fees": view["fees"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—"),
            "Currency": view["currency"],
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")
    st.download_button(
        "Download normalized CSV",
        data=transactions.to_csv(index=False).encode("utf-8-sig"),
        file_name="portfolio_normalized.csv",
        mime="text/csv",
    )


def _degiro_page(selected: pd.DataFrame | None, book: dict[str, object] | None, broker: str) -> None:
    st.markdown('<div class="pdt-kicker">Costs</div>', unsafe_allow_html=True)
    if broker not in {"all", "degiro"}:
        st.info("Switch broker filter to All brokers or DeGiro to open this page.")
        return
    if selected is None or selected.empty:
        st.info("No transactions available. Add CSV exports and process data.")
        return
    degiro = selected.loc[selected["broker"].eq("degiro")].copy()
    if degiro.empty:
        st.info("No DEGIRO rows found in this selection.")
        return
    if "venue" not in degiro.columns or degiro["venue"].fillna("").eq("").all():
        st.caption("Execution venues are empty. Re-import the DEGIRO Transactions.csv file to fill Beurs.")

    _degiro_fee_section(degiro)
    st.divider()
    _degiro_venue_section(degiro, book)


def _degiro_fee_section(degiro: pd.DataFrame) -> None:
    st.subheader("Transaction costs")
    trades = degiro.loc[degiro["type"].isin(["buy", "sell"])].copy()
    if trades.empty:
        st.info("No DEGIRO buys or sells were found.")
        return
    trades["fees"] = pd.to_numeric(trades["fees"], errors="coerce").fillna(0.0)
    trades["amount_num"] = pd.to_numeric(trades["amount"], errors="coerce")
    trades["cash_traded"] = trades["amount_num"].abs()
    trades["year"] = pd.to_datetime(trades["date"], errors="coerce").dt.year
    totals = _fee_metrics(trades)
    a, b, c = st.columns(3)
    a.metric("Total costs", _eur(totals["fees"]))
    b.metric("Cash traded", _eur(totals["cash"]))
    b.caption("Buys and sells with a recorded cash amount.")
    c.metric("Cost rate", _pct(totals["rate"], 3))

    by_year = (
        trades.dropna(subset=["year"])
        .groupby("year", sort=True, as_index=False)
        .apply(_fee_metrics_group)
        .reset_index(drop=True)
    )
    by_etf = (
        trades.assign(key=trades["isin"].where(trades["isin"].fillna("").ne(""), trades["symbol"]))
        .groupby(["key", "name"], sort=False, as_index=False)
        .apply(_fee_metrics_group)
        .reset_index(drop=True)
        .rename(columns={"key": "isin_or_symbol"})
        .sort_values("fees", ascending=False, kind="mergesort")
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**By year**")
        if by_year.empty:
            st.caption("No yearly view available yet.")
        else:
            st.dataframe(
                pd.DataFrame(
                    {
                        "Year": by_year["year"].astype(int),
                        "Costs": by_year["fees"].map(_eur),
                        "Cash traded": by_year["cash"].map(_eur),
                        "Rate": by_year["rate"].map(lambda value: _pct(value, 3)),
                    }
                ),
                hide_index=True,
                width="stretch",
            )
    with right:
        st.markdown("**By ETF**")
        if by_etf.empty:
            st.caption("No ETF rows available yet.")
        else:
            st.dataframe(
                pd.DataFrame(
                    {
                        "ISIN / Symbol": by_etf["isin_or_symbol"],
                        "Name": by_etf["name"],
                        "Costs": by_etf["fees"].map(_eur),
                        "Cash traded": by_etf["cash"].map(_eur),
                        "Rate": by_etf["rate"].map(lambda value: _pct(value, 3)),
                    }
                ),
                hide_index=True,
                width="stretch",
            )


def _fee_metrics(trades: pd.DataFrame) -> dict[str, float]:
    fees = float(pd.to_numeric(trades["fees"], errors="coerce").fillna(0.0).sum())
    cash = float(pd.to_numeric(trades["cash_traded"], errors="coerce").dropna().sum())
    rate = fees / cash * 100 if cash > 0 else 0.0
    return {"fees": fees, "cash": cash, "rate": rate}


def _fee_metrics_group(group: pd.DataFrame) -> pd.Series:
    totals = _fee_metrics(group)
    row = {column: group.iloc[0][column] for column in group.columns if column in {"year", "name", "key"}}
    row.update(totals)
    return pd.Series(row)


def _degiro_venue_section(degiro: pd.DataFrame, book: dict[str, object] | None) -> None:
    st.subheader("ETF spread by execution venue")
    balances, warnings = degiro_venue_balances(degiro)
    if balances.empty:
        st.info("No venue balances found.")
        return
    balances = balances.copy()
    totals = balances.groupby("isin", sort=False)["quantity"].sum().rename("total_quantity")
    balances = balances.merge(totals, on="isin", how="left")
    balances["share"] = balances["quantity"] / balances["total_quantity"] * 100

    prices = _degiro_prices(book)
    balances["price"] = balances["isin"].map(prices)
    balances["value"] = balances["quantity"] * balances["price"]
    table = pd.DataFrame(
        {
            "ISIN": balances["isin"],
            "Name": balances["name"],
            "Venue": balances["venue"],
            "Quantity": balances["quantity"].map(_qty),
            "Share": balances["share"].map(lambda value: _pct(float(value), 2)),
            "Value": balances["value"].map(lambda value: _eur(float(value)) if pd.notna(value) else "—"),
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")
    if warnings:
        st.caption(" ".join(sorted(set(warnings))))


def _degiro_prices(book: dict[str, object] | None) -> dict[str, float]:
    if not book:
        return {}
    portfolio = book.get("portfolio")
    if not isinstance(portfolio, Portfolio) or portfolio.positions.empty:
        return {}
    positions = portfolio.positions.loc[portfolio.positions["broker"].eq("degiro")].copy()
    if positions.empty:
        return {}
    positions["price_num"] = pd.to_numeric(positions["price"], errors="coerce")
    values = {}
    for row in positions.itertuples(index=False):
        isin = str(getattr(row, "isin", "") or "").strip().upper()
        price = getattr(row, "price_num", math.nan)
        if isin and pd.notna(price):
            values[isin] = float(price)
    return values


def _include_crypto() -> bool:
    return bool(st.session_state.get("include_crypto", True))


def _scoped_totals(portfolio: Portfolio) -> tuple[float, float, float]:
    total = portfolio.net_worth - portfolio.cash_eur
    capital = portfolio.invested
    # Keep the headline consistent with account-level gain/loss:
    # holdings value minus net invested capital, plus cash income/fees.
    net_gain = (total - capital) + portfolio.income + portfolio.fees
    if _include_crypto() or portfolio.holdings.empty:
        return capital, total, net_gain
    visible = portfolio.holdings.loc[~_crypto_mask(portfolio.holdings)].copy()
    if visible.empty:
        return 0.0, 0.0, 0.0
    quantity = pd.to_numeric(visible["quantity"], errors="coerce").fillna(0)
    avg_cost = pd.to_numeric(visible["avg_cost"], errors="coerce").fillna(0)
    capital_visible = float((quantity * avg_cost).sum())
    total_visible = float(pd.to_numeric(visible["current_value"], errors="coerce").fillna(0).sum())
    return capital_visible, total_visible, total_visible - capital_visible


def _crypto_mask(frame: pd.DataFrame) -> pd.Series:
    kind = (
        frame["security_type"].astype(str).str.strip().str.lower()
        if "security_type" in frame.columns
        else pd.Series("", index=frame.index)
    )
    sector = (
        frame["sector"].astype(str).str.strip().str.lower()
        if "sector" in frame.columns
        else pd.Series("", index=frame.index)
    )
    broker = (
        frame["broker"].astype(str).str.strip().str.lower()
        if "broker" in frame.columns
        else pd.Series("", index=frame.index)
    )
    return kind.eq("crypto") | sector.eq("crypto") | broker.isin(["bitvavo", "ledger"])


def _coin_label(value: object) -> str:
    text = str(value or "").strip()
    return text or "Unknown"


def _group_label(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return "Not set"
    return TYPE_LABELS.get(text, text)


def _holding_name(row: pd.Series) -> str:
    name = str(row.get("name") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    kind = str(row.get("security_type") or "").strip().lower()
    if kind in {"equity", "etf", "fund"} and name and name.upper() != symbol.upper():
        return name
    return symbol or name or "Holding"


def _style(figure, title: str, height: int = 420) -> None:
    dark = bool(st.session_state.get("dark", True))
    ink = "#F4F7F6" if dark else "#14221F"
    grid = "#1C3A36" if dark else "#E3E6E1"
    figure.update_layout(
        title={"text": title, "x": 0, "font": {"size": 16, "color": ink}},
        height=height,
        margin={"l": 8, "r": 8, "t": 48, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": ink, "size": 13},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "font": {"color": ink, "size": 13}},
        colorway=PALETTE,
    )
    figure.update_xaxes(showgrid=False, color=ink)
    figure.update_yaxes(gridcolor=grid, color=ink)


def _price_sources(portfolio: Portfolio) -> tuple[dict[str, int], dict[str, str]]:
    counts = {"live": 0, "cache": 0, "csv": 0, "cost": 0, "unpriced": 0}
    by_key: dict[str, str] = {}
    if portfolio.positions.empty:
        return counts, by_key
    for row in portfolio.positions.itertuples(index=False):
        key = str(getattr(row, "isin", "") or "") or str(getattr(row, "symbol", "") or "")
        if not key:
            continue
        if bool(getattr(row, "unpriced", False)):
            source = "unpriced"
        elif bool(getattr(row, "valued_at_cost", False)):
            source = "cost"
        else:
            source = str(getattr(row, "price_source", "") or "csv")
            if source not in counts:
                source = "csv"
        counts[source] += 1
        if key in by_key:
            by_key[key] = _merge_sources(by_key[key], source)
        else:
            by_key[key] = source
    return counts, by_key


def _attach_price_source(holdings: pd.DataFrame, by_key: dict[str, str], positions: pd.DataFrame) -> pd.DataFrame:
    frame = holdings.copy()
    if frame.empty:
        frame["price_source"] = ""
        frame["price_as_of"] = ""
        return frame
    stamp_by_key: dict[str, str] = {}
    for row in positions.itertuples(index=False):
        key = str(getattr(row, "isin", "") or "") or str(getattr(row, "symbol", "") or "")
        if not key:
            continue
        stamp = str(getattr(row, "price_as_of", "") or "")
        if stamp and (key not in stamp_by_key or stamp_by_key[key] < stamp):
            stamp_by_key[key] = stamp
    values = []
    stamps = []
    for row in frame.itertuples(index=False):
        key = str(getattr(row, "isin", "") or "") or str(getattr(row, "symbol", "") or "")
        values.append(by_key.get(key, "cost"))
        stamps.append(stamp_by_key.get(key, ""))
    frame["price_source"] = values
    frame["price_as_of"] = stamps
    return frame


def _merge_sources(left: str, right: str) -> str:
    if left == right:
        return left
    order = {"live": 5, "cache": 4, "csv": 3, "cost": 2, "unpriced": 1}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _holding_price_source_label(row: pd.Series) -> str:
    source = str(row.get("price_source") or "").strip().lower()
    if source == "csv":
        stamp = _short_date(str(row.get("price_as_of") or ""))
        if stamp:
            return f"Last trade ({stamp})"
    return _group_label(source)


def _holding_price_label(row: pd.Series) -> str:
    source = str(row.get("price_source") or "").strip().lower()
    value = row.get("current_value")
    quantity = row.get("quantity")
    if source == "unpriced" or pd.isna(value) or pd.isna(quantity) or abs(float(quantity)) < 1e-9:
        return "—"
    return _eur(float(value) / float(quantity))


def _holding_value_label(row: pd.Series) -> str:
    source = str(row.get("price_source") or "").strip().lower()
    value = row.get("current_value")
    if source == "unpriced":
        return "Unpriced"
    if pd.notna(value):
        return _eur(float(value))
    return "—"


def _short_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _mark_etf_rollups(holdings: pd.DataFrame) -> pd.DataFrame:
    if holdings.empty:
        return holdings
    composition = load_etf_composition()
    if not composition:
        return holdings
    keys = set(composition.keys())
    frame = holdings.copy()
    for index, row in frame.iterrows():
        security_type = str(row.get("security_type") or "").strip().lower()
        key = str(row.get("isin") or "").strip().upper() or str(row.get("symbol") or "").strip().upper()
        if security_type in {"etf", "fund"} and key in keys:
            frame.at[index, "sector"] = "Several"
            frame.at[index, "country"] = "Several countries"
    return frame


def _qty(value: object) -> str:
    if pd.isna(value):
        return "—"
    number = round(float(value), 2)
    if number == int(number):
        return _fmt_number(number, 0)
    return _fmt_number(number, 2)


def _fmt_number(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _eur(value: float) -> str:
    if _hidden_money():
        return "***"
    sign = "-" if value < 0 else ""
    return f"{sign}€ {_fmt_number(abs(value), 2)}"


def _pct(value: float, digits: int = 2) -> str:
    return f"{_fmt_number(value, digits)}%"


def _data_menu(meta: dict) -> None:
    when = _imported_at(meta.get("imported_at", ""))
    with st.popover("Data found and used"):
        if when:
            st.caption(when)
        files = meta.get("files", [])
        if not files:
            st.write("No files recorded.")
            return
        for item in files:
            count = int(item.get("rows", 0))
            label = LABELS.get(item.get("source", ""), item.get("source", ""))
            noun = "row" if count == 1 else "rows"
            st.markdown(f"**{label}** · {count} {noun} in {item.get('name', '')}")


def _imported_at(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d %b %Y, %H:%M")


def _empty_state() -> None:
    st.info("Add CSV exports in the sidebar and press Process, or use the sample data.")
    degiro, bitvavo, ledger = st.columns(3)
    degiro.markdown("**DEGIRO**  \nTransactions or account statement")
    bitvavo.markdown("**Bitvavo**  \nTransaction history")
    ledger.markdown("**Ledger**  \nLedger Live operations")


if __name__ == "__main__":
    main()
