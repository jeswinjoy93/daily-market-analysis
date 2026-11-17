"""Daily Market Analysis web app.

    python3 server.py            # http://localhost:8050

While running, it also fetches automatically on weekdays: a live (provisional) snapshot every
LIVE_REFRESH_MIN minutes during market hours, then from 17:00 every 30 min until that day's
final end-of-day data is stored.
"""
import json
import os
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import market

PORT = int(os.environ.get("PORT", 8050))
STATIC = market.BASE / "static"
LIVE_REFRESH_MIN = int(os.environ.get("LIVE_REFRESH_MIN", 15))  # each refresh is ~60 Trendvue requests, ~1 min
FETCH_LOCK = threading.Lock()  # one fetch at a time (Fetch now vs the automatic refresh)


def snapshot():
    if not FETCH_LOCK.acquire(blocking=False):
        with FETCH_LOCK:  # another fetch is running: wait for it and show its result instead of fetching twice
            return {"stored": False, "reason": "An automatic refresh was already running; showing its result."}
    try:
        with closing(market.connect()) as con:
            return market.take_snapshot(con)
    finally:
        FETCH_LOCK.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/compare":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                with closing(market.connect()) as con:
                    res = market.compare(con, q.get("date"), q.get("metric", market.DEFAULT_METRIC))
                return self._json(res)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/snapshot":
            try:
                return self._json(snapshot())
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": f"Fetch failed: {e}"}, 502)
        self.send_error(404)

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            super().log_message(fmt, *args)


def auto_snapshot_loop():
    """Schedule in market.snapshot_due: every LIVE_REFRESH_MIN during market hours, then every
    30 min from 17:00 until the day's final data is stored."""
    while True:
        now = datetime.now()
        try:
            with closing(market.connect()) as con:
                due = market.snapshot_due(con, now)
            if due:
                print("[auto] snapshot:", snapshot(), flush=True)
        except Exception as e:
            print("[auto] snapshot failed:", e, flush=True)
        time.sleep(LIVE_REFRESH_MIN * 60 if market.in_market_hours(now) else 1800)


if __name__ == "__main__":
    threading.Thread(target=auto_snapshot_loop, daemon=True).start()
    print(f"Daily Market Analysis → http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
