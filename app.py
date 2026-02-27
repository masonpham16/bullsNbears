import os
if os.environ.get("RENDER"):
    socketio.start_background_task(poll_loop)
import time
import requests
from dotenv import load_dotenv

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

# eventlet makes SocketIO easy for a first project
#socketio = SocketIO(app, cors_allowed_origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

PROVIDER = os.getenv("MARKET_API_PROVIDER", "finnhub").lower()
API_KEY = os.getenv("FINNHUB_API_KEY", "")
SYMBOL = os.getenv("SYMBOL", "AAPL")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))

latest = {
    "symbol": SYMBOL,
    "ts": None,
    "price": None,
    "prev_close": None,
    "change": None,
    "change_pct": None,
    "note": "starting up",
}

def fetch_quote_finnhub(symbol: str) -> dict:
    """
    Finnhub Quote endpoint:
    https://finnhub.io/docs/api/quote
    Returns:
      c: current price
      pc: previous close
      d: change
      dp: percent change
      t: timestamp (unix)
    """
    if not API_KEY:
        raise RuntimeError("Missing FINNHUB_API_KEY in .env")

    url = "https://finnhub.io/api/v1/quote"
    r = requests.get(url, params={"symbol": symbol, "token": API_KEY}, timeout=10)
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

def poll_loop():
    global latest
    while True:
        try:
            if PROVIDER == "finnhub":
                latest = fetch_quote_finnhub(SYMBOL)
            else:
                latest = {**latest, "note": f"Unknown provider: {PROVIDER}"}

            # Push to all connected browsers
            socketio.emit("tick", latest)
        except Exception as e:
            latest = {**latest, "note": f"error: {type(e).__name__}: {e}"}
            socketio.emit("tick", latest)

        time.sleep(POLL_SECONDS)

@app.route("/")
def index():
    return render_template("index.html", symbol=SYMBOL, poll_seconds=POLL_SECONDS)

@app.route("/api/latest")
def api_latest():
    return jsonify(latest)

@socketio.on("connect")
def on_connect():
    # Send whatever we currently have immediately
    socketio.emit("tick", latest)

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    # Start background polling task
    socketio.start_background_task(poll_loop)
#    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
    # socketio.run(app, host="0.0.0.0", port=5050, debug=True) # do this when debugging
    socketio.run(app, host="0.0.0.0", port=5050, debug=False)
