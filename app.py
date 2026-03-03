"""
app.py — Live Stock Display (Flask + Socket.IO)

- Emits a `tick` event per symbol with:
  symbol, ts, price, prev_close, change, change_pct, volume, note
- Volume is daily volume in SHARES, pulled from Finnhub candle endpoint.
- Caches volume to avoid hammering the API.
- Staggers requests to avoid bursty traffic.

Local dev and Azure prod: async_mode="threading" with gunicorn threads.
"""

import os
import time
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
try:
    from flask_socketio import SocketIO
    SOCKETIO_AVAILABLE = True
except Exception:
    SOCKETIO_AVAILABLE = False

    class SocketIO:  # fallback so app can still start if socketio deps fail
        def __init__(self, app, **kwargs):
            self._app = app

        def on(self, _event):
            def decorator(func):
                return func

            return decorator

        def emit(self, *_args, **_kwargs):
            return None

        def sleep(self, seconds):
            time.sleep(seconds)

        def start_background_task(self, target, *args, **kwargs):
            import threading

            t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            t.start()
            return t

        def run(self, app, host="0.0.0.0", port=5050, debug=False):
            app.run(host=host, port=port, debug=debug)

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

raw_async_mode = (os.getenv("SOCKETIO_ASYNC_MODE", "threading") or "").strip().lower()
if raw_async_mode not in {"threading", "eventlet", "gevent", "gevent_uwsgi"}:
    raw_async_mode = "threading"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode=raw_async_mode,
    logger=False,
    engineio_logger=False,
)

PROVIDER = os.getenv("MARKET_API_PROVIDER", "finnhub").lower()
API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Quotes refresh cadence
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))

# Volume refresh cadence (per symbol)
VOLUME_TTL_SECONDS = int(os.getenv("VOLUME_TTL_SECONDS", "90"))
AVG_VOLUME_TTL_SECONDS = int(os.getenv("AVG_VOLUME_TTL_SECONDS", "21600"))

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
latest_by_symbol = {
    sym: {
        "symbol": sym,
        "ts": None,
        "price": None,
        "prev_close": None,
        "change": None,
        "change_pct": None,
        "volume": None,  # shares
        "avg_volume": None,  # average daily shares
        "note": "starting up",
    }
    for sym in SYMBOLS
}

# Cache: symbol -> {"value": int|None, "fetched_at": unix_seconds}
volume_cache = {sym: {"value": None, "fetched_at": 0} for sym in SYMBOLS}
avg_volume_cache = {sym: {"value": None, "fetched_at": 0} for sym in SYMBOLS}


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


def fetch_intraday_volume_finnhub(symbol: str):
    """
    Pull today's running volume by summing 1-minute candle volumes.
    This is usually more reliable intraday than daily candle snapshots.
    """
    _ensure_api_key()

    now = int(time.time())
    _from = now - 24 * 3600

    r = requests.get(
        "https://finnhub.io/api/v1/stock/candle",
        params={
            "symbol": symbol,
            "resolution": "1",
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

    return int(sum(vols))


def fetch_daily_volume_finnhub(symbol: str):
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


def fetch_intraday_volume_yahoo(symbol: str):
    """
    Fallback volume source using Yahoo chart data.
    Sums intraday volume bars for a 1-day range.
    """
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "1d", "interval": "1m"},
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    data = r.json()

    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None

    indicators = result.get("indicators") or {}
    quote = (indicators.get("quote") or [None])[0]
    if not quote:
        return None

    vols = quote.get("volume") or []
    vals = [int(v) for v in vols if isinstance(v, (int, float))]
    if not vals:
        return None

    return int(sum(vals))


def fetch_average_daily_volume_finnhub(symbol: str):
    """
    Average recent daily volume for baseline "normal activity".
    """
    _ensure_api_key()

    now = int(time.time())
    _from = now - 60 * 24 * 3600

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

    vols = [int(v) for v in (data.get("v") or []) if isinstance(v, (int, float))]
    if not vols:
        return None

    sample = vols[-20:]
    if not sample:
        return None

    return int(sum(sample) / len(sample))


def fetch_average_daily_volume_yahoo(symbol: str):
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "3mo", "interval": "1d"},
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()
    data = r.json()

    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None

    indicators = result.get("indicators") or {}
    quote = (indicators.get("quote") or [None])[0]
    if not quote:
        return None

    vols = quote.get("volume") or []
    vals = [int(v) for v in vols if isinstance(v, (int, float))]
    if not vals:
        return None

    sample = vals[-40:]
    return int(sum(sample) / len(sample))


def get_cached_volume(symbol: str):
    """Return cached volume (shares), refreshing if stale."""
    now = int(time.time())
    entry = volume_cache.get(symbol, {"value": None, "fetched_at": 0})

    if (now - int(entry.get("fetched_at", 0))) < VOLUME_TTL_SECONDS:
        return entry.get("value")

    # Refresh using provider fallbacks; one failure should not block others.
    v = None
    try:
        v = fetch_intraday_volume_finnhub(symbol)
    except Exception:
        v = None

    if v is None:
        try:
            v = fetch_daily_volume_finnhub(symbol)
        except Exception:
            v = None

    if v is None:
        try:
            v = fetch_intraday_volume_yahoo(symbol)
        except Exception:
            v = None

    if v is None:
        v = entry.get("value")

    volume_cache[symbol] = {"value": v, "fetched_at": now}
    return v


def get_cached_avg_volume(symbol: str):
    now = int(time.time())
    entry = avg_volume_cache.get(symbol, {"value": None, "fetched_at": 0})

    if (now - int(entry.get("fetched_at", 0))) < AVG_VOLUME_TTL_SECONDS:
        return entry.get("value")

    v = None
    try:
        v = fetch_average_daily_volume_finnhub(symbol)
    except Exception:
        v = None

    if v is None:
        try:
            v = fetch_average_daily_volume_yahoo(symbol)
        except Exception:
            v = None

    if v is None:
        v = entry.get("value")

    avg_volume_cache[symbol] = {"value": v, "fetched_at": now}
    return v


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
                    avg_vol = get_cached_avg_volume(sym)
                    latest_by_symbol[sym] = {**q, "volume": vol, "avg_volume": avg_vol}

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
    print("client connected")

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
    port = int(os.getenv("PORT", "5050"))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
