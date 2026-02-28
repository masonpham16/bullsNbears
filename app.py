"""
app.py — Live Stock Display (Flask + Socket.IO)

- Emits a `tick` event per symbol with:
  symbol, ts, price, prev_close, change, change_pct, volume, note
- Volume is daily volume in SHARES, pulled from Finnhub candle endpoint.
- Caches volume to avoid hammering the API.
- Staggers requests to avoid bursty traffic.

Local dev: async_mode="threading" is fine.
Azure prod (recommended): Python 3.12 + eventlet worker via gunicorn.
"""

import os
import time
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode=os.getenv("SOCKETIO_ASYNC_MODE", "threading"),  # "threading" locally
    logger=True,
    engineio_logger=True,
)

PROVIDER = os.getenv("MARKET_API_PROVIDER", "finnhub").lower()
API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Quotes refresh cadence
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))

# Volume refresh cadence (per symbol)
VOLUME_TTL_SECONDS = int(os.getenv("VOLUME_TTL_SECONDS", "300"))  # default 5 minutes

# Small delay between symbol requests to avoid bursty spikes
QUOTE_STAGGER_MS = int(os.getenv("QUOTE_STAGGER_MS", "150"))

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL"]
SYMBOLS = [
    s.strip().upper()
    for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",")
    if s.strip()
]


def _ensure_api_key():
    if not API_KEY:
        raise RuntimeError("Missing FINNHUB_API_KEY in .env")


# Latest quote per symbol
latest_by_symbol: dict[str, dict] = {
    sym: {
        "symbol": sym,
        "ts": None,
        "price": None,
        "prev_close": None,
        "change": None,
        "change_pct": None,
        "volume": None,  # shares
        "note": "starting up",
    }
    for sym in SYMBOLS
}

# Cache: symbol -> {"value": int|None, "fetched_at": unix_seconds}
volume_cache: dict[str, dict] = {sym: {"value": None, "fetched_at": 0} for sym in SYMBOLS}


def fetch_quote_finnhub(symbol: str) -> dict:
    """
    Finnhub Quote endpoint:
    https://finnhub.io/docs/api/quote

    Returns:
      c: current price
      pc: previous close
      d: change
      dp: percent change
      t: timestamp (unix seconds)
    """
    _ensure_api_key()

    r = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    q = r.json()

    return {
        "symbol": symbol,
        "ts": q.get("t"),
        "price": q.get("c"),
        "prev_close": q.get("pc"),
        "change": q.get("d"),
        "change_pct": q.get("dp"),
        "note": "ok",
    }


def fetch_daily_volume_finnhub(symbol: str) -> int | None:
    """
    Pull today's (most recent daily candle) volume in SHARES via /stock/candle.
    https://finnhub.io/docs/api/stock-candles
    """
    _ensure_api_key()

    now = int(time.time())
    _from = now - 2 * 24 * 3600  # 2-day buffer for timezone/market-close edges

    r = requests.get(
        "https://finnhub.io/api/v1/stock/candle",
        params={
            "symbol": symbol,
            "resolution": "D",
            "from": _from,
            "to": now,
            "token": API_KEY,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("s") != "ok":
        return None

    vols = data.get("v") or []
    if not vols:
        return None

    return int(vols[-1])


def get_cached_volume(symbol: str) -> int | None:
    """Return cached volume (shares), refreshing if stale."""
    now = int(time.time())
    entry = volume_cache.get(symbol, {"value": None, "fetched_at": 0})

    if (now - int(entry.get("fetched_at", 0))) < VOLUME_TTL_SECONDS:
        return entry.get("value")

    # Refresh; if refresh fails, keep old cached value
    try:
        v = fetch_daily_volume_finnhub(symbol)
        volume_cache[symbol] = {"value": v, "fetched_at": now}
        return v
    except Exception:
        return entry.get("value")


def poll_loop():
    """
    Background task: fetch quotes (+ cached volume) and emit `tick` updates.

    API-friendly behavior:
    - Always fetch quote each cycle (fast “live” feel)
    - Refresh volume only when TTL expires
    - Stagger symbol requests to reduce burstiness
    """
    while True:
        cycle_start = time.time()

        for sym in SYMBOLS:
            try:
                if PROVIDER != "finnhub":
                    latest_by_symbol[sym] = {
                        **latest_by_symbol[sym],
                        "note": f"Unknown provider: {PROVIDER}",
                    }
                else:
                    q = fetch_quote_finnhub(sym)
                    vol = get_cached_volume(sym)  # shares (int) or None
                    latest_by_symbol[sym] = {**q, "volume": vol}

            except Exception as e:
                latest_by_symbol[sym] = {
                    **latest_by_symbol[sym],
                    "note": f"error: {type(e).__name__}: {e}",
                }

            socketio.emit("tick", latest_by_symbol[sym])

            # Spread requests a bit
            socketio.sleep(QUOTE_STAGGER_MS / 1000.0)

        # Keep the cycle close to POLL_SECONDS overall
        elapsed = time.time() - cycle_start
        remaining = max(0.0, POLL_SECONDS - elapsed)
        socketio.sleep(remaining)


_thread_started = False


@socketio.on("connect")
def handle_connect():
    global _thread_started
    print("🔥 client connected")

    if not _thread_started:
        _thread_started = True
        socketio.start_background_task(poll_loop)

    # Immediate snapshot to the new client
    socketio.emit("server_test", {"ok": True})
    for sym in SYMBOLS:
        socketio.emit("tick", latest_by_symbol[sym])


@app.route("/")
def index():
    return render_template("index.html", symbols=SYMBOLS, poll_seconds=POLL_SECONDS)


@app.route("/api/latest")
def api_latest():
    return jsonify({"symbols": SYMBOLS, "latest": latest_by_symbol})


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050, debug=False)
