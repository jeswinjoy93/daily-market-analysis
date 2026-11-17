"""Fetch Trendvue sector/industry breadth (live during market hours), store snapshots in SQLite, and compare days.

Usage:
    python3 market.py snapshot      # fetch now: live (provisional) during market hours, final after Trendvue's EOD update
    python3 market.py auto          # fetch only if due (market hours, or evening until the day's final data is in)
    python3 market.py compare       # print latest vs previous in the terminal
    python3 market.py export DIR    # write the dashboard as a static site (index.html + data/*.json)
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MARKET_DB", BASE / "data" / "market.db"))
API = "https://trendvue.in/api/segment_analysis"
SOURCES = {"sector": f"{API}/sector_performance", "industry": f"{API}/industry_performance"}
STOCKS_URL = f"{API}/industry_stocks/"  # + industry name; per stock: live 1D/1W/1M moves, EOD pct_from_52w_high
# One row per stock, as of the last end-of-day update: market cap (rupees), exchange (NSE:/BSE: for TradingView),
# that session's close, 52-week high, % from it and 1D/1W/1M moves.
LISTING_URL = ("https://trendvue.in/api/screener-data/stocks"
               "?fields=market_cap_basic,close,price_52_week_high,pct_from_52w_high,1D,1W,1M")
MIN_MCAP_CR = 1000  # universe: only stocks with market cap >= this (₹ crore), for both the % and the stock lists
MIN_GROUP_STOCKS = 3  # sectors/industries need at least this many stocks in the universe to be ranked
MAX_DIST_FROM_HIGH = 10  # stock lists / TradingView export: stocks within this % of their 52-week high, closest first
# A liquid stock's daily bars (the last one is live during market hours) tell us which session each source reflects.
PROBE_SYMBOL = "RELIANCE"
PROBE_URL = f"https://trendvue.in/api/eod/{PROBE_SYMBOL}?days=3"

# Automatic fetch schedule, local time (IST; NSE trades 09:15-15:30).
MARKET_HOURS = ((9, 15), (15, 45))  # live refreshes; the last one after 15:30 records the closing prices
AUTO_AFTER_HOUR = 17  # from then on, fetch until the day's final end-of-day data is stored

# "% of stocks within N% of their 52-week high" — the metric on the Trendvue page.
METRICS = ["near_52w_2", "near_52w_5", "near_52w_10", "near_52w_15", "near_52w_20"]
DEFAULT_METRIC = "near_52w_10"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    snap_date   TEXT NOT NULL UNIQUE,      -- trading session the data reflects, YYYY-MM-DD
    fetched_at  TEXT NOT NULL,
    data_hash   TEXT NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 0 -- 1 = live, intraday prices; replaced by the final data after the close
);
CREATE TABLE IF NOT EXISTS segment_values (
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,            -- 'sector' | 'industry'
    name         TEXT NOT NULL,
    sector       TEXT,                     -- parent sector (industries only)
    stock_count  INTEGER,
    near_52w_2   REAL, near_52w_5 REAL, near_52w_10 REAL, near_52w_15 REAL, near_52w_20 REAL,
    raw          TEXT NOT NULL,            -- full JSON row, for metrics added later
    PRIMARY KEY (snapshot_id, kind, name)
);
CREATE TABLE IF NOT EXISTS stock_values (
    snapshot_id        INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    symbol             TEXT NOT NULL,
    company            TEXT,
    industry           TEXT NOT NULL,
    sector             TEXT,
    pct_from_52w_high  REAL,                -- 0 = at the high, -8.5 = 8.5% below it
    change_1d          REAL,                -- % moves over 1 day / 1 week (5 sessions) / 1 month (21 sessions)
    change_1w          REAL,
    change_1m          REAL,
    market_cap_cr      REAL,                -- ₹ crore
    exchange           TEXT,                -- NSE | BSE
    price              REAL,                -- price used for pct_from_52w_high (live price in a provisional snapshot)
    high_52w           REAL,
    PRIMARY KEY (snapshot_id, symbol, industry)
);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(stock_values)")}
    if "market_cap_cr" not in cols:  # databases created before the market-cap filter
        con.execute("ALTER TABLE stock_values ADD COLUMN market_cap_cr REAL")
    for col in ("exchange TEXT", "price REAL", "high_52w REAL", "change_1w REAL", "change_1m REAL"):
        if col.split()[0] not in cols:
            con.execute(f"ALTER TABLE stock_values ADD COLUMN {col}")
    if "provisional" not in {r["name"] for r in con.execute("PRAGMA table_info(snapshots)")}:
        con.execute("ALTER TABLE snapshots ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0")
    return con


REQUEST_GAP = 1.0  # seconds between stock-list calls; Trendvue allows 120 requests/min per IP (shared with your browser)


def fetch_json(url, retries=4):
    """GET JSON, waiting out HTTP 429 rate limits (Retry-After) before retrying."""
    req = urllib.request.Request(url, headers={"User-Agent": "daily-market-analysis/1.0", "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if attempt == retries or not (e.code == 429 or e.code >= 500):
                raise
            if e.code == 429:
                wait = e.headers.get("Retry-After") or e.headers.get("RateLimit-Reset") or "10"
                time.sleep(min(int(float(wait)) + 1, 65))
            else:  # 5xx: the server hiccupped
                time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, ConnectionError, TimeoutError):  # connection refused/reset, timeouts
            if attempt == retries:
                raise
            time.sleep(3 * (attempt + 1))


def _mcap_cr(listing_row):
    v = (listing_row or {}).get("market_cap_basic")
    return round(v / 1e7, 1) if v else None


def _pct_change(a, b):
    return (a / b - 1) * 100


def _sessions(listing, lists_1d):
    """Work out which session each source reflects, using the probe stock's daily bars.

    Returns (date, live, base_date):
      live=False: the listing is already updated for the latest session `date` (after Trendvue's EOD refresh).
      live=True:  the listing is still the previous session `base_date`; the stock lists' 1D is today's live move.
    """
    bars = fetch_json(PROBE_URL)
    last, prev = bars[-1], bars[-2]
    lst, d1 = listing.get(PROBE_SYMBOL) or {}, lists_1d.get(PROBE_SYMBOL)
    move = _pct_change(last["close"], prev["close"])
    near = lambda x, y, tol: x is not None and abs(x - y) <= tol
    if near(lst.get("close"), last["close"], 0.01) and near(lst.get("1D"), move, 0.05):
        return last["date"][:10], False, None
    if near(lst.get("close"), prev["close"], 0.01) and near(d1, move, 0.05):
        return last["date"][:10], True, prev["date"][:10]
    raise RuntimeError("The data source is mid-update (its feeds disagree on the session); try again in a few minutes")


def _digest(stocks):
    return hashlib.sha256(json.dumps(stocks, sort_keys=True).encode()).hexdigest()


def _store(con, snap_date, provisional, stocks, segments=None):
    digest = _digest(stocks)
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM snapshots WHERE snap_date = ?", (snap_date,))
        sid = con.execute(
            "INSERT INTO snapshots (snap_date, fetched_at, data_hash, provisional) VALUES (?, ?, ?, ?)",
            (snap_date, now, digest, int(provisional)),
        ).lastrowid
        for kind, rows in (segments or {}).items():  # Trendvue's own numbers, kept for reference
            con.executemany(
                f"INSERT INTO segment_values (snapshot_id, kind, name, sector, stock_count, {', '.join(METRICS)}, raw)"
                f" VALUES (?, ?, ?, ?, ?, {', '.join('?' * len(METRICS))}, ?)",
                [(sid, kind, r["name"], r.get("sector"), r.get("stock_count"), *[r.get(m) for m in METRICS], json.dumps(r))
                 for r in rows],
            )
        con.executemany(
            "INSERT OR REPLACE INTO stock_values (snapshot_id, symbol, company, industry, sector, pct_from_52w_high,"
            " change_1d, change_1w, change_1m, market_cap_cr, exchange, price, high_52w) VALUES (:sid, :symbol, :company,"
            " :industry, :sector, :pct, :change_1d, :change_1w, :change_1m, :mcap, :exchange, :price, :high)",
            [{"sid": sid, **st} for st in stocks],
        )


def take_snapshot(con=None):
    """Fetch everything and store it under the session it reflects.

    During market hours (and after the close until Trendvue's end-of-day refresh) Trendvue's
    52W-high numbers are still the previous session's, but each stock's 1D move is live. Then:
        live price = previous close x (1 + live 1D)    distance = live price / max(52W high, live price) - 1
    and the result is stored as a provisional snapshot for today. Once the end-of-day data is out,
    a fetch stores Trendvue's own final numbers, replacing the provisional one. All-or-nothing:
    any failure raises before the database is touched.
    """
    con = con or connect()
    segments = {kind: fetch_json(url) for kind, url in SOURCES.items()}
    if not segments["sector"]:
        raise RuntimeError("The data source returned no sector data")
    listing = {r["symbol"]: r for r in fetch_json(LISTING_URL)}

    members = []  # (industry, sector, stock row); a sector's stocks = union of its industries'
    for ind in segments["industry"]:
        try:
            rows = fetch_json(STOCKS_URL + urllib.parse.quote(ind["name"], safe=""))
        except Exception as e:
            raise RuntimeError(f"Stock list for '{ind['name']}' failed ({e}); existing data left unchanged") from e
        members += [(ind["name"], ind.get("sector"), r) for r in rows]
        time.sleep(REQUEST_GAP)

    snap_date, live, base_date = _sessions(listing, {r["name"]: r.get("1D") for _, _, r in members})

    def stock(ind, sec, r, pct, moves, price, high):  # moves: the row carrying 1D/1W/1M (live list row or listing)
        lst = listing.get(r["name"]) or {}
        return {"symbol": r["name"], "company": r.get("company_name"), "industry": ind, "sector": sec,
                "pct": pct, "change_1d": moves.get("1D"), "change_1w": moves.get("1W"), "change_1m": moves.get("1M"),
                "mcap": _mcap_cr(lst), "exchange": lst.get("exchange"), "price": price, "high": high}

    def final(listing_moves):  # the session the listing reflects, with Trendvue's own % from 52W high
        out = []
        for ind, sec, r in members:
            lst = listing.get(r["name"]) or {}
            out.append(stock(ind, sec, r, lst.get("pct_from_52w_high", r.get("pct_from_52w_high")),
                             lst if listing_moves else r, lst.get("close"), lst.get("price_52_week_high")))
        return out

    if not live:
        stocks = final(listing_moves=False)
        have = con.execute("SELECT data_hash FROM snapshots WHERE snap_date = ? AND provisional = 0", (snap_date,)).fetchone()
        if have and have["data_hash"] == _digest(stocks):  # e.g. a market holiday: nothing new since the last fetch
            return {"stored": False, "live": False, "date": snap_date, "reason": f"Already up to date ({snap_date})."}
        _store(con, snap_date, False, stocks, segments)
        return {"stored": True, "live": False, "date": snap_date, "sectors": len(segments["sector"]),
                "industries": len(segments["industry"]), "stocks": len(stocks)}

    stocks = []
    for ind, sec, r in members:
        lst = listing.get(r["name"]) or {}
        close, high, d1 = lst.get("close"), lst.get("price_52_week_high"), r.get("1D")
        price = pct = None
        if close and high and d1 is not None:
            price = close * (1 + d1 / 100)
            high = max(high, price)  # a new 52W high today
            pct = round(_pct_change(price, high), 2)
        stocks.append(stock(ind, sec, r, pct, r, None if price is None else round(price, 2), high))

    # The listing still holds the previous session's final data: store that session too if we don't have it final.
    base = con.execute("SELECT provisional FROM snapshots WHERE snap_date = ?", (base_date,)).fetchone()
    if base is None or base["provisional"]:
        _store(con, base_date, False, final(listing_moves=True), segments)
    _store(con, snap_date, True, stocks)
    return {"stored": True, "live": True, "date": snap_date, "base_date": base_date,
            "sectors": len(segments["sector"]), "industries": len(segments["industry"]), "stocks": len(stocks)}


def top_stocks(con, snap_date, n=None, min_mcap_cr=MIN_MCAP_CR, max_dist=MAX_DIST_FROM_HIGH):
    """Per sector and per industry: stocks closest to their 52-week high (all, or the first n),
    among companies with market cap >= min_mcap_cr (unknown market cap is excluded)
    that are no more than max_dist % below their 52-week high."""
    rows = con.execute(
        "SELECT st.symbol, st.company, st.industry, st.sector, st.pct_from_52w_high AS dist, st.change_1d, st.change_1w, st.change_1m,"
        " st.market_cap_cr, st.exchange FROM stock_values st JOIN snapshots s ON s.id = st.snapshot_id"
        " WHERE s.snap_date = ? AND st.pct_from_52w_high IS NOT NULL AND st.market_cap_cr >= ? AND st.pct_from_52w_high >= ?"
        " ORDER BY st.pct_from_52w_high DESC, st.symbol",
        (snap_date, min_mcap_cr, -max_dist),
    ).fetchall()
    out = {"sector": {}, "industry": {}}
    seen = set()
    for r in rows:
        d = dict(r)
        ind = out["industry"].setdefault(r["industry"], [])
        if n is None or len(ind) < n:
            ind.append(d)
        if r["sector"] and (r["sector"], r["symbol"]) not in seen:
            seen.add((r["sector"], r["symbol"]))
            sec = out["sector"].setdefault(r["sector"], [])
            if n is None or len(sec) < n:
                sec.append(d)
    return out


def list_dates(con):
    return [r["snap_date"] for r in con.execute("SELECT snap_date FROM snapshots ORDER BY snap_date DESC")]


def live_dates(con):
    return [r["snap_date"] for r in con.execute("SELECT snap_date FROM snapshots WHERE provisional = 1")]


def _values(con, snap_date, kind, metric, min_mcap_cr=MIN_MCAP_CR):
    """% of stocks within X% of their 52W high, per sector or industry, computed from stock-level data
    over the universe of stocks with market cap >= min_mcap_cr. (Without the cap filter this
    reproduces Trendvue's own near_52w_X numbers exactly.) stock_count = size of that universe."""
    within = int(metric.rsplit("_", 1)[1])
    if kind == "sector":
        group, cols = "sector", "sector AS name, NULL AS sector"
    else:
        group, cols = "industry, sector", "industry AS name, sector"
    rows = con.execute(
        f"SELECT {cols}, COUNT(*) AS stock_count,"
        f" ROUND(100.0 * SUM(pct >= ?) / COUNT(*), 1) AS value FROM ("
        f"   SELECT DISTINCT v.{'sector' if kind == 'sector' else 'industry, v.sector'}, v.symbol,"
        f"   v.pct_from_52w_high AS pct FROM stock_values v JOIN snapshots s ON s.id = v.snapshot_id"
        f"   WHERE s.snap_date = ? AND v.market_cap_cr >= ? AND v.pct_from_52w_high IS NOT NULL"
        f" ) GROUP BY {group}",
        (-within, snap_date, min_mcap_cr),
    )
    return {r["name"]: dict(r) for r in rows if r["name"]}


def _diff(cur, prev):
    out = []
    for name, c in cur.items():
        p = prev.get(name)
        pv = p["value"] if p else None
        change = round(c["value"] - pv, 2) if (pv is not None and c["value"] is not None) else None
        out.append({**c, "prev": pv, "change": change})
    out.sort(key=lambda r: (r["change"] is None, -(r["change"] or 0)))
    return out


def compare(con, snap_date=None, metric=DEFAULT_METRIC, top_n=5):
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    dates = list_dates(con)
    if not dates:
        return {"date": None, "prev_date": None, "dates": [], "metric": metric}
    snap_date = snap_date if snap_date in dates else dates[0]
    older = [d for d in dates if d < snap_date]
    prev_date = older[0] if older else None

    def side(kind):
        return _diff(_values(con, snap_date, kind, metric), _values(con, prev_date, kind, metric) if prev_date else {})

    sectors, industries = side("sector"), side("industry")
    def rankable(rows):
        return [s for s in rows if s["change"] is not None and (s["stock_count"] or 0) >= MIN_GROUP_STOCKS]

    changed, ind_changed = rankable(sectors), rankable(industries)
    snap = con.execute("SELECT fetched_at, provisional FROM snapshots WHERE snap_date = ?", (snap_date,)).fetchone()
    return {
        "date": snap_date,
        "live": bool(snap["provisional"]),  # intraday prices; final numbers replace it after the close
        "fetched_at": snap["fetched_at"],
        "prev_date": prev_date,
        "dates": dates,
        "live_dates": live_dates(con),
        "metric": metric,
        "strength": [s for s in changed if s["change"] > 0][:top_n],
        "industry_strength": [s for s in ind_changed if s["change"] > 0][:top_n],
        "weakness": sorted([s for s in changed if s["change"] < 0], key=lambda s: s["change"])[:top_n],
        "sectors": sectors,
        "industries": industries,
        "top_stocks": top_stocks(con, snap_date),
        "min_group_stocks": MIN_GROUP_STOCKS,
        "min_mcap_cr": MIN_MCAP_CR,  # applies to the sector/industry % and to the stock lists
        "max_dist_from_high": MAX_DIST_FROM_HIGH,
    }


def _print_compare(res):
    if not res["date"]:
        print("No snapshots yet. Run: python3 market.py snapshot")
        return
    print(f"{res['metric']}: {res['date']} vs {res['prev_date'] or '(no previous day)'}")
    for title, rows in (("Strength", res["strength"]), ("Weakness", res["weakness"])):
        print(f"\nTop {title}:")
        for r in rows:
            print(f"  {r['name']:<40} {r['prev']:>6.1f} -> {r['value']:>6.1f}  ({r['change']:+.1f} pts)")
        if not rows:
            print("  (none)")


def in_market_hours(now):
    return now.weekday() < 5 and MARKET_HOURS[0] <= (now.hour, now.minute) <= MARKET_HOURS[1]


def snapshot_due(con, now=None):
    """Whether an automatic fetch should run now: every time during market hours, and on weekday
    evenings until that day's final (non-provisional) data is stored."""
    now = now or datetime.now()
    if in_market_hours(now):
        return True
    if now.weekday() < 5 and now.hour >= AUTO_AFTER_HOUR:
        row = con.execute("SELECT provisional FROM snapshots WHERE snap_date = ?", (now.strftime("%Y-%m-%d"),)).fetchone()
        return not row or bool(row["provisional"])
    return False


def export_site(con, out_dir):
    """Write the dashboard as a static site: index.html plus data/index.json (sessions) and
    data/<date>.json per session, holding every threshold's numbers, so no server is needed."""
    out, data_dir = Path(out_dir), Path(out_dir) / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    dates = list_dates(con)
    per_metric = ("strength", "industry_strength", "sectors", "industries")
    for d in dates:
        by_metric = {m: compare(con, d, m) for m in METRICS}
        first = by_metric[METRICS[0]]
        doc = {k: v for k, v in first.items() if k not in per_metric + ("weakness", "metric", "dates", "live_dates")}
        doc["by_metric"] = {m: {k: r[k] for k in per_metric} for m, r in by_metric.items()}
        (data_dir / f"{d}.json").write_text(json.dumps(doc, separators=(",", ":")))
    (data_dir / "index.json").write_text(json.dumps({
        "dates": dates, "live_dates": live_dates(con), "generated_at": datetime.now().isoformat(timespec="seconds")}))
    page = (BASE / "static" / "index.html").read_text()
    marker = "<script>window.STATIC_SITE = false;</script>"
    assert marker in page, "static/index.html is missing the STATIC_SITE marker"
    (out / "index.html").write_text(page.replace(marker, "<script>window.STATIC_SITE = true;</script>"))
    return len(dates)


def _github_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in kv.items())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    con = connect()
    if cmd == "snapshot":
        res = take_snapshot(con)
        print(json.dumps(res))
        _github_output(changed=str(res["stored"]).lower())
    elif cmd == "auto":
        if not snapshot_due(con):
            print("Not due: outside market hours, or today's final data is already stored.")
            _github_output(changed="false")
        else:
            try:
                res = take_snapshot(con)
            except RuntimeError as e:
                if "mid-update" not in str(e):
                    raise
                res = {"stored": False, "reason": str(e)}  # transient: the next scheduled run picks it up
            print(json.dumps(res))
            _github_output(changed=str(res["stored"]).lower())
    elif cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "site"
        print(f"Exported {export_site(con, out)} sessions to {out}/")
    elif cmd == "compare":
        _print_compare(compare(con, metric=sys.argv[2] if len(sys.argv) > 2 else DEFAULT_METRIC))
    else:
        sys.exit(__doc__)
