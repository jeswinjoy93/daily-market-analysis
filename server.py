"""Daily Market Analysis web app (India and US).

    python3 server.py            # http://localhost:8050

While running, it also fetches each market automatically on its weekdays: a live (provisional) snapshot
every LIVE_REFRESH_MIN minutes during that market's hours, then every 30 min after its close until that
day's final data is stored (see market.MARKETS and market.snapshot_due). The Setups scans (India) are
fetched on the same loop when due (market.setups_due).
"""
import json
import os
import threading
import time
import traceback
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import market

PORT = int(os.environ.get("PORT", 8050))
STATIC = market.BASE / "static"
LIVE_REFRESH_MIN = int(os.environ.get("LIVE_REFRESH_MIN", market.REFRESH_MIN))  # India: ~60 Trendvue requests (~1 min); US: 1 request
FETCH_LOCK = threading.Lock()  # one fetch at a time (manual vs the automatic refresh)


def snapshot(m="in"):
    if not FETCH_LOCK.acquire(blocking=False):
        with FETCH_LOCK:  # another fetch is running: wait for it instead of fetching twice
            return {"stored": False, "reason": "An automatic refresh was already running; showing its result."}
    try:
        return market.take_market_snapshot(m)
    finally:
        FETCH_LOCK.release()


def refresh_setups():
    """Fetch the Setups scans (India); shares the fetch lock, since both use Trendvue's rate limit."""
    with FETCH_LOCK, closing(market.connect("in")) as con:
        return market.take_setups(con)


def _market(q):
    m = q.get("market", "in")
    if m not in market.MARKETS:
        raise ValueError(f"market must be one of {list(market.MARKETS)}")
    return m


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
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/markets":
            return self._json([{"id": m, "name": cfg["name"]} for m, cfg in market.MARKETS.items()])
        if url.path == "/api/compare":
            try:
                m = _market(q)
                with closing(market.connect(m)) as con:
                    res = market.compare(con, q.get("date"), q.get("metric", market.DEFAULT_METRIC), market=m)
                return self._json(res)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if url.path == "/api/setups":
            try:
                with closing(market.connect("in")) as con:
                    res = market.setups(con)
                if not res["date"]:  # nothing stored yet: fetch now
                    refresh_setups()
                    with closing(market.connect("in")) as con:
                        res = market.setups(con)
                return self._json(res)
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": f"Setups fetch failed: {e}"}, 502)
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/snapshot":
            try:
                return self._json(snapshot(_market({k: v[0] for k, v in parse_qs(url.query).items()})))
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": f"Fetch failed: {e}"}, 502)
        self.send_error(404)

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            super().log_message(fmt, *args)


def auto_snapshot_loop():
    while True:
        busy = False
        for m in market.MARKETS:
            try:
                with closing(market.connect(m)) as con:
                    due = market.snapshot_due(con, market=m)
                if due:
                    print(f"[auto {m}] snapshot:", snapshot(m), flush=True)
            except Exception as e:
                print(f"[auto {m}] snapshot failed:", e, flush=True)
            busy = busy or market.in_market_hours(market=m)
        try:
            with closing(market.connect("in")) as con:
                due = market.setups_due(con)
            if due:
                print("[auto setups]", refresh_setups(), flush=True)
        except Exception as e:
            print("[auto setups] failed:", e, flush=True)
        time.sleep(LIVE_REFRESH_MIN * 60 if busy else 1800)


if __name__ == "__main__":
    threading.Thread(target=auto_snapshot_loop, daemon=True).start()
    print(f"Daily Market Analysis → http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
