import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from io import BytesIO, StringIO

import anthropic
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "signals.db")

# In-memory dataframe cache
_df_cache = {"df": None, "file_path": None, "mapping": None}


# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            row_id       TEXT PRIMARY KEY,
            headline     TEXT,
            ticker       TEXT,
            date         TEXT,
            source       TEXT,
            signal       TEXT,
            reasoning    TEXT,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


# ─── DataFrame helpers ───────────────────────────────────────────────────────

def make_row_id(ticker, date, headline):
    raw = f"{ticker}|{date}|{headline}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_df():
    """Load (or return cached) dataframe using saved file path + column mapping."""
    file_path = get_setting("file_path")
    mapping_json = get_setting("column_mapping")
    if not file_path or not mapping_json or not os.path.exists(file_path):
        return None, None

    mapping = json.loads(mapping_json)

    if (
        _df_cache["df"] is not None
        and _df_cache["file_path"] == file_path
        and _df_cache["mapping"] == mapping
    ):
        return _df_cache["df"], mapping

    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path, dtype=str)
    else:
        df = pd.read_csv(file_path, dtype=str)

    df = df.fillna("")

    # Rename to standard internal names
    rename = {v: k for k, v in mapping.items() if v}
    df = df.rename(columns=rename)

    # Ensure required columns exist
    for col in ("headline", "ticker", "date", "source"):
        if col not in df.columns:
            df[col] = ""

    df["row_id"] = df.apply(
        lambda r: make_row_id(r["ticker"], r["date"], r["headline"]), axis=1
    )

    _df_cache["df"] = df
    _df_cache["file_path"] = file_path
    _df_cache["mapping"] = mapping
    return df, mapping


def clear_df_cache():
    _df_cache["df"] = None
    _df_cache["file_path"] = None
    _df_cache["mapping"] = None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        data = request.json or {}
        if "api_key" in data:
            set_setting("api_key", data["api_key"])
        if "model" in data:
            set_setting("model", data["model"])
        if "column_mapping" in data:
            set_setting("column_mapping", json.dumps(data["column_mapping"]))
            clear_df_cache()
        return jsonify({"ok": True})

    api_key = get_setting("api_key", "")
    model = get_setting("model", "claude-haiku-4-5")
    mapping_json = get_setting("column_mapping")
    mapping = json.loads(mapping_json) if mapping_json else {}
    file_path = get_setting("file_path", "")
    return jsonify({
        "api_key_set": bool(api_key),
        "model": model,
        "column_mapping": mapping,
        "file_path": os.path.basename(file_path) if file_path else "",
    })


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    filename = f.filename
    if not filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".csv", ".xlsx", ".xls"):
        return jsonify({"error": "Only CSV and Excel files are supported"}), 400

    save_path = os.path.join(BASE_DIR, filename)
    f.save(save_path)

    # Read column names only
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(save_path, nrows=0)
    else:
        df = pd.read_csv(save_path, nrows=0)

    set_setting("file_path", save_path)
    clear_df_cache()

    return jsonify({"columns": list(df.columns), "filename": filename})


@app.route("/api/data")
def get_data():
    df, mapping = load_df()
    if df is None:
        return jsonify({"rows": [], "total": 0, "sources": [], "tickers": []})

    # Filters
    ticker_q = request.args.get("ticker", "").strip().lower()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    source_q = request.args.get("source", "").strip()
    signal_q = request.args.get("signal", "all").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    filtered = df.copy()

    if ticker_q:
        filtered = filtered[filtered["ticker"].str.lower().str.contains(ticker_q, na=False)]
    if source_q and source_q != "all":
        filtered = filtered[filtered["source"] == source_q]
    if date_from:
        filtered = filtered[filtered["date"] >= date_from]
    if date_to:
        filtered = filtered[filtered["date"] <= date_to]

    # Load signals from DB
    conn = get_db()
    sigs = conn.execute("SELECT row_id, signal, reasoning FROM signals").fetchall()
    conn.close()
    sig_map = {r["row_id"]: {"signal": r["signal"], "reasoning": r["reasoning"]} for r in sigs}

    filtered["_signal"] = filtered["row_id"].map(lambda rid: sig_map.get(rid, {}).get("signal", ""))
    filtered["_reasoning"] = filtered["row_id"].map(lambda rid: sig_map.get(rid, {}).get("reasoning", ""))

    # Signal filter
    if signal_q == "unprocessed":
        filtered = filtered[filtered["_signal"] == ""]
    elif signal_q in ("buy", "neutral", "sell", "error"):
        filtered = filtered[filtered["_signal"] == signal_q]

    total = len(filtered)
    unprocessed = int((filtered["_signal"] == "").sum())

    # Unique sources and tickers for dropdowns
    all_sources = sorted(df["source"].dropna().unique().tolist())
    all_tickers = sorted(df["ticker"].dropna().unique().tolist())

    # Paginate
    start = (page - 1) * per_page
    page_df = filtered.iloc[start: start + per_page]

    rows = []
    for _, r in page_df.iterrows():
        rows.append({
            "row_id": r["row_id"],
            "headline": r["headline"],
            "ticker": r["ticker"],
            "date": r["date"],
            "source": r["source"],
            "signal": r["_signal"],
            "reasoning": r["_reasoning"],
        })

    return jsonify({
        "rows": rows,
        "total": total,
        "unprocessed": unprocessed,
        "page": page,
        "per_page": per_page,
        "sources": all_sources,
        "tickers": all_tickers,
    })


@app.route("/api/process", methods=["POST"])
def process_signals():
    api_key = get_setting("api_key", "")
    if not api_key:
        return jsonify({"error": "API key not configured. Go to Settings to add your Anthropic API key."}), 400

    model = get_setting("model", "claude-haiku-4-5")
    data = request.json or {}
    row_ids = data.get("row_ids", [])

    if not row_ids:
        return jsonify({"processed": 0, "results": []})

    # Build a map of row_id -> row data from cached df
    df, _ = load_df()
    if df is None:
        return jsonify({"error": "No data loaded"}), 400

    id_to_row = df.set_index("row_id")[["headline", "ticker"]].to_dict("index")

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = (
        "You are a financial analyst assistant. Classify news headlines by their likely "
        "short-term market impact on the named company's stock.\n\n"
        "- 'buy'     = clearly positive catalyst (earnings beat, major contract, upgrade, acquisition premium)\n"
        "- 'sell'    = clearly negative catalyst (earnings miss, lawsuit, downgrade, fraud, bankruptcy)\n"
        "- 'neutral' = no clear directional signal or ambiguous impact\n"
        "- When uncertain, default to 'neutral'\n"
        "- Focus only on the specific company named, not general sector/macro news\n\n"
        'Respond ONLY in this exact JSON format: {"signal": "buy"|"neutral"|"sell", "reason": "one sentence max"}'
    )

    results = []
    conn = get_db()

    for row_id in row_ids:
        if row_id not in id_to_row:
            results.append({"row_id": row_id, "error": "row not found"})
            continue

        headline = id_to_row[row_id]["headline"]
        ticker = id_to_row[row_id]["ticker"]

        signal = None
        reasoning = ""
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=120,
                    system=system_prompt,
                    messages=[{
                        "role": "user",
                        "content": f"Company: {ticker}\nHeadline: {headline}"
                    }]
                )
                text = response.content[0].text.strip()
                parsed = json.loads(text)
                signal = parsed.get("signal", "neutral").lower()
                if signal not in ("buy", "neutral", "sell"):
                    signal = "neutral"
                reasoning = parsed.get("reason", "")
                break
            except anthropic.RateLimitError:
                wait = 5 * (2 ** attempt)
                time.sleep(wait)
            except Exception as e:
                signal = "error"
                reasoning = str(e)
                break

        if signal is None:
            signal = "error"
            reasoning = "Rate limit exceeded after retries"

        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO signals
               (row_id, headline, ticker, date, source, signal, reasoning, processed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (row_id, headline, ticker, "", "", signal, reasoning, now)
        )
        conn.commit()
        results.append({"row_id": row_id, "signal": signal})
        time.sleep(0.4)

    conn.close()
    return jsonify({"processed": len(results), "results": results})


@app.route("/api/export")
def export_data():
    df, _ = load_df()
    if df is None:
        return jsonify({"error": "No data loaded"}), 400

    ticker_q = request.args.get("ticker", "").strip().lower()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    source_q = request.args.get("source", "").strip()
    signal_q = request.args.get("signal", "all").strip()

    filtered = df.copy()
    if ticker_q:
        filtered = filtered[filtered["ticker"].str.lower().str.contains(ticker_q, na=False)]
    if source_q and source_q != "all":
        filtered = filtered[filtered["source"] == source_q]
    if date_from:
        filtered = filtered[filtered["date"] >= date_from]
    if date_to:
        filtered = filtered[filtered["date"] <= date_to]

    conn = get_db()
    sigs = conn.execute("SELECT row_id, signal, reasoning FROM signals").fetchall()
    conn.close()
    sig_map = {r["row_id"]: {"signal": r["signal"], "reasoning": r["reasoning"]} for r in sigs}

    filtered["signal"] = filtered["row_id"].map(lambda rid: sig_map.get(rid, {}).get("signal", ""))
    filtered["signal_reasoning"] = filtered["row_id"].map(lambda rid: sig_map.get(rid, {}).get("reasoning", ""))

    if signal_q == "unprocessed":
        filtered = filtered[filtered["signal"] == ""]
    elif signal_q in ("buy", "neutral", "sell", "error"):
        filtered = filtered[filtered["signal"] == signal_q]

    export_cols = ["headline", "ticker", "date", "source", "signal", "signal_reasoning"]
    out = filtered[[c for c in export_cols if c in filtered.columns]]

    buf = BytesIO()
    out.to_csv(buf, index=False, encoding="utf-8")
    buf.seek(0)

    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="headlines_export.csv"
    )


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("\n  STOXX 600 Sentiment Analyzer")
    print("  Open http://localhost:8080 in your browser\n")
    app.run(debug=False, port=8080)
