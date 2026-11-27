"""Sector/industry breadth for India (Trendvue) and the US (TradingView screener, see us.py): fetch (live during
market hours), store snapshots in SQLite (one database per market), and compare days.

Usage (MARKET = in | us, default in; MARKETS = comma-separated, e.g. in,us):
    python3 market.py snapshot [MARKETS]      # fetch now: live (provisional) during market hours, final after the close
    python3 market.py auto [MARKETS]          # fetch each market only if due (market hours, or after the close until final)
                                              # (with India, both also fetch the Setups scans: see take_setups)
    python3 market.py setups                  # fetch the Setups scans (India) now
    python3 market.py triage [force]          # notes on the latest setups' stocks now (see triage.py)
    python3 market.py compare [METRIC] [MARKET]   # print latest vs previous in the terminal
    python3 market.py export DIR [MARKETS]    # static site (index.html + data/<market>/*.json); default: markets with data
    python3 market.py next-wait [MARKETS]     # seconds until the next update is due (used by the GitHub workflow)
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
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
# A liquid stock's daily bars (the last one is live during market hours) tell us which session each source reflects.
PROBE_SYMBOL = "RELIANCE"
PROBE_URL = f"https://trendvue.in/api/eod/{PROBE_SYMBOL}?days=5"
EOD_URL = "https://trendvue.in/api/eod/"  # + symbol; daily bars (?timeframe=weekly for weekly) with EMA 20/50/200
# Setups page (India): each of the SETUP_SCANS Trendvue daily scans, keeping only the stocks that are also in the
# SETUP_FILTER scan, in the filter scan's order (Tight Consolidation: tightest first).
SCANS_URL = "https://trendvue.in/api/daily_scans/"  # + scan name + "/enriched"
SETUP_SCANS = {"high_tight_flags_stocks": "High Tight Flag", "near_ATH": "Near All-Time High", "top_movers": "Top Movers"}
SETUP_FILTER = ("consolidation", "Tight Consolidation")
SETUP_ABOVE_EMA = "ema50"  # second filter: last close above this EMA (ema20/ema50/ema200), from the daily bars; None = off
MOVER_PERIODS = ("1d", "1w", "1m", "3m", "1y")  # Top Movers rows flag the periods they lead: is_1d, is_1w, ...
CHART_BARS = 260  # bars per chart and timeframe: covers Trendvue's longest ranges (daily 1Y = 252, weekly 5Y = 260)
REFRESH_MIN = 5  # minutes between live updates (GitHub workflow and local server)
LIVE_PROBE_TOLERANCE = 1.0  # % points between the live 1D and today's still-forming bar (refreshed on separate cycles)

# Per-market settings. Times are the market's local time. min_mcap is in the unit stored in
# stock_values.market_cap_cr: ₹ crore for India, US$ million for the US.
MARKETS = {
    "in": {"name": "India", "db": DB_PATH, "tz": "Asia/Kolkata",
           "hours": ((9, 15), (15, 50)),   # live refreshes (NSE 09:15-15:30); the last one records the closing prices
           "final_after": (17, 0),         # then fetch until the day's final end-of-day data is stored
           "min_mcap": MIN_MCAP_CR, "mcap_label": "₹1,000 cr"},
    "us": {"name": "US", "db": Path(os.environ.get("MARKET_DB_US", BASE / "data" / "market_us.db")), "tz": "America/New_York",
           "hours": ((9, 30), (16, 5)),    # NYSE/NASDAQ 09:30-16:00 ET; TradingView's free data is 15 min delayed
           "final_after": (16, 20),        # closing prices are in by then
           "min_mcap": 1000, "mcap_label": "$1B"},
}
MARKET_HOURS = MARKETS["in"]["hours"]  # India (kept for older callers)
AUTO_AFTER_HOUR = MARKETS["in"]["final_after"][0]


def market_now(market, now=None):
    """`now` (aware, or naive = this machine's local time) in the market's own timezone."""
    tz = ZoneInfo(MARKETS[market]["tz"])
    return (now or datetime.now().astimezone()).astimezone(tz)

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
CREATE TABLE IF NOT EXISTS setups (            -- India: each SETUP_SCANS scan filtered by SETUP_FILTER
    snap_date   TEXT PRIMARY KEY,              -- session the scans reflect
    fetched_at  TEXT NOT NULL,
    data_hash   TEXT NOT NULL,
    live        INTEGER NOT NULL DEFAULT 0,    -- fetched during market hours: prices and the last bar are intraday
    data        TEXT NOT NULL,                 -- JSON: the setups (scan sizes, symbols) and their stocks
    charts      TEXT                           -- JSON: daily and weekly bars per stock; latest session only
);
CREATE TABLE IF NOT EXISTS triage (            -- India: notes on the setups' stocks and their sectors/industries (triage.py)
    snap_date   TEXT NOT NULL,                 -- the setups session
    kind        TEXT NOT NULL,                 -- 'stock' | 'sector' | 'industry'
    name        TEXT NOT NULL,                 -- symbol, or sector/industry name
    fetched_at  TEXT NOT NULL,
    data        TEXT NOT NULL,                 -- JSON: facts, filings, news, and the DeepSeek notes (null without a key)
    PRIMARY KEY (snap_date, kind, name)
);
"""


def connect(market="in"):
    path = MARKETS[market]["db"]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
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


def fetch_json(url, retries=4, body=None):
    """GET JSON (or POST `body` as JSON), waiting out HTTP 429 rate limits (Retry-After) before retrying."""
    headers = {"User-Agent": "daily-market-analysis/1.0", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=None if body is None else json.dumps(body).encode())
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
    """Work out which session each source reflects. Returns (date, live, base_date):
      live=False: the listing is already updated for the latest session `date` (after Trendvue's EOD refresh).
      live=True:  the listing is still the previous session `base_date`; the stock lists' 1D is today's live move.

    Each source's 1D move for the probe stock is matched to the daily bar with the same move, which dates it.
    (Matching prices is unreliable: the latest bar's close is derived from the move, e.g. 1367.56 vs an official 1367.0.)
    The listing counts as caught up only when its 1D also agrees with the live lists for nearly every stock.
    """
    bars = fetch_json(PROBE_URL)
    moves = [(bars[i]["date"][:10], _pct_change(bars[i]["close"], bars[i - 1]["close"])) for i in range(1, len(bars))]
    dates = [d for d, _ in moves]

    def session_of(d1):  # latest bar whose day move matches
        return next((d for d, mv in reversed(moves) if d1 is not None and abs(d1 - mv) <= 0.05), None)

    listing_date = session_of((listing.get(PROBE_SYMBOL) or {}).get("1D"))
    lists_date = session_of(lists_1d.get(PROBE_SYMBOL))
    both = [(listing[s]["1D"], d1) for s, d1 in lists_1d.items()
            if d1 is not None and (listing.get(s) or {}).get("1D") is not None]
    agree = sum(abs(a - b) <= 0.05 for a, b in both) / max(len(both), 1)
    if listing_date and listing_date == lists_date and agree >= 0.9:
        return listing_date, False, None
    # Live: the lists have moved on to the session after the listing's. During market hours that session's bar is
    # itself in progress and its close refreshes on a different cycle from the lists' 1D, so the two drift apart by
    # more than the exact-match tolerance (often on flat days); only require them to roughly agree.
    nxt = dates.index(listing_date) + 1 if listing_date else None
    live_1d = lists_1d.get(PROBE_SYMBOL)
    if (nxt is not None and nxt < len(moves) and agree < 0.9
            and (live_1d is None or abs(live_1d - moves[nxt][1]) <= LIVE_PROBE_TOLERANCE)):
        return dates[nxt], True, listing_date
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


def _one_per_name(rows):
    """One row per group name. Trendvue's industry list can repeat an industry (from 2026-11-27, each one appears once
    with its sector and once with sector null): keep the first, or the repeat if only it has the sector."""
    out = {}
    for r in rows or []:
        have = out.get(r["name"])
        if have is None or (not have.get("sector") and r.get("sector")):
            out[r["name"]] = r
    return list(out.values())


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
    segments = {kind: _one_per_name(fetch_json(url)) for kind, url in SOURCES.items()}
    if not segments["sector"]:
        raise RuntimeError("The data source returned no sector data")
    # An industry without a sector (from 2026-11-27, Banks): the sector it was last stored under, so its stocks still
    # count toward their sector.
    known = {r["name"]: r["sector"] for r in con.execute(
        "SELECT v.name, v.sector FROM segment_values v JOIN snapshots s ON s.id = v.snapshot_id"
        " WHERE v.kind = 'industry' AND v.sector IS NOT NULL ORDER BY s.snap_date")}
    for ind in segments["industry"]:
        if not ind.get("sector") and known.get(ind["name"]):
            ind["sector"] = known[ind["name"]]
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


def _within(metric):  # "near_52w_10" -> 10
    return int(metric.rsplit("_", 1)[1])


def top_stocks(con, snap_date, max_dist, n=None, min_mcap_cr=MIN_MCAP_CR):
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
    within = _within(metric)
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


def compare(con, snap_date=None, metric=DEFAULT_METRIC, top_n=5, market="in"):
    cfg = MARKETS[market]
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    dates = list_dates(con)
    if not dates:
        return {"date": None, "prev_date": None, "dates": [], "metric": metric, "market": market, "market_name": cfg["name"]}
    snap_date = snap_date if snap_date in dates else dates[0]
    older = [d for d in dates if d < snap_date]
    prev_date = older[0] if older else None

    def side(kind):
        return _diff(_values(con, snap_date, kind, metric, cfg["min_mcap"]),
                     _values(con, prev_date, kind, metric, cfg["min_mcap"]) if prev_date else {})

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
        # Stock lists follow the selected threshold, so each list's length = the count behind its %.
        "top_stocks": top_stocks(con, snap_date, _within(metric), min_mcap_cr=cfg["min_mcap"]),
        "min_group_stocks": MIN_GROUP_STOCKS,
        "market": market,
        "market_name": cfg["name"],
        "mcap_label": cfg["mcap_label"],  # universe: applies to the sector/industry % and to the stock lists
        "max_dist_from_high": _within(metric),
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


def in_market_hours(now=None, market="in"):
    t = market_now(market, now)
    return t.weekday() < 5 and MARKETS[market]["hours"][0] <= (t.hour, t.minute) <= MARKETS[market]["hours"][1]


def snapshot_due(con, now=None, market="in"):
    """Whether an automatic fetch should run now: every time during market hours, and on weekdays after
    the close (final_after) until that day's final (non-provisional) data is stored."""
    t = market_now(market, now)
    if in_market_hours(t, market):
        return True
    if t.weekday() < 5 and (t.hour, t.minute) >= MARKETS[market]["final_after"]:
        row = con.execute("SELECT provisional FROM snapshots WHERE snap_date = ?", (t.strftime("%Y-%m-%d"),)).fetchone()
        return not row or bool(row["provisional"])
    return False


def next_wait_seconds(markets=("in",), now=None, step_min=REFRESH_MIN, cap=5 * 3600 + 45 * 60):
    """Seconds until the next update is due for any of `markets`, for the self-scheduling GitHub workflow:
    the next REFRESH_MIN-minute slot during a market's weekday hours or after its close (until midnight, its time),
    else the next market open. Capped below GitHub's 6-hour job limit; a capped wait just starts a run that
    finds nothing due and waits again."""
    now = (now or datetime.now().astimezone()).astimezone(ZoneInfo("UTC"))
    def active(t):
        for m in markets:
            lt, cfg = market_now(m, t), MARKETS[m]
            if lt.weekday() < 5 and (cfg["hours"][0] <= (lt.hour, lt.minute) <= cfg["hours"][1]
                                     or (lt.hour, lt.minute) >= cfg["final_after"]):
                return True
        return False
    t = (now + timedelta(minutes=step_min - now.minute % step_min)).replace(second=0, microsecond=0)
    while not active(t):
        t += timedelta(minutes=step_min)
    return min(int((t - now).total_seconds()) + 60, cap)  # +1 min: start just after the slot


def take_market_snapshot(market):
    con = connect(market)
    try:
        if market == "us":
            import us
            return us.take_snapshot(con)
        return take_snapshot(con)
    finally:
        con.close()


BAR_FIELDS = ("date", "open", "high", "low", "close", "volume", "ema20", "ema50", "ema200")


def _bars(symbol, timeframe="daily", n=CHART_BARS):
    """Trendvue's bars for a symbol as compact rows [date, open, high, low, close, volume, ema20, ema50, ema200].
    During market hours the last daily (and weekly) bar is still forming."""
    q = f"?days={n}" + ("&timeframe=weekly" if timeframe == "weekly" else "")
    rows = fetch_json(EOD_URL + urllib.parse.quote(symbol, safe="") + q) or []
    return [[r.get("date", "")[:10], *(int(r[k]) if k == "volume" and r.get(k) is not None else r.get(k)
                                       for k in BAR_FIELDS[1:])] for r in rows][-n:]


def _bar_session(bars, d1):
    """Date of the latest daily bar whose day move matches `d1` (a scan row's 1D %), or None.
    Scan rows carry live prices during market hours, but their 1D is the session the scan was run on."""
    if d1 is None:
        return None
    for i in range(len(bars) - 1, 0, -1):
        if bars[i][4] and bars[i - 1][4] and abs(_pct_change(bars[i][4], bars[i - 1][4]) - d1) <= 0.05:
            return bars[i][0]
    return None


def take_setups(con, now=None):
    """Fetch the SETUP_SCANS scans and the SETUP_FILTER scan, and store each setup (its scan's stocks that are also in
    the filter scan and, with SETUP_ABOVE_EMA, close above that EMA) with the charts of every stock in any setup,
    under the session the scans reflect.
    Unchanged data (same session, same prices and bars) is not stored again."""
    lists = {}
    for name in (*SETUP_SCANS, SETUP_FILTER[0]):
        lists[name] = fetch_json(f"{SCANS_URL}{name}/enriched") or []
        time.sleep(REQUEST_GAP)
    filt = lists[SETUP_FILTER[0]]
    setup_list = []
    for name, label in SETUP_SCANS.items():
        members = {r["symbol"] for r in lists[name]}
        setup_list.append({"id": name, "name": label, "count": len(lists[name]),
                           "symbols": [r["symbol"] for r in filt if r["symbol"] in members]})
    in_any = {sym for st in setup_list for sym in st["symbols"]}
    movers = {r["symbol"]: [p.upper() for p in MOVER_PERIODS if r.get(f"is_{p}")] for r in lists.get("top_movers", [])}

    known = {}  # exchange, sector and industry from the latest India snapshot that has the stock
    for r in con.execute("SELECT v.symbol, v.exchange, v.sector, v.industry FROM stock_values v"
                         " JOIN snapshots s ON s.id = v.snapshot_id ORDER BY s.snap_date"):
        known[r["symbol"]] = dict(r)

    stocks, charts = [], {}
    for r in filt:  # the filter scan's row: it has the consolidation columns
        sym = r["symbol"]
        if sym not in in_any:
            continue
        charts[sym] = {"daily": _bars(sym)}
        time.sleep(REQUEST_GAP)
        charts[sym]["weekly"] = _bars(sym, "weekly")
        time.sleep(REQUEST_GAP)
        k = known.get(sym, {})
        stocks.append({"symbol": sym, "company": r.get("company_name"), "exchange": k.get("exchange") or "NSE",
                       "sector": k.get("sector"), "industry": k.get("industry"),
                       "close": r.get("close"), "change_1d": r.get("change"), "change_1w": r.get("1W"),
                       "change_1m": r.get("1M"), "dist": r.get("pct_from_52w_high"), "ath": r.get("pct_from_ath"),
                       "rs_rating": r.get("rs_rating"), "consolidation_days": r.get("consolidation_days"),
                       "tightness": r.get("tightness"), "mcap": _mcap_cr(r), "movers": movers.get(sym)})

    # Date the scans by a stock's 1D move: a setup stock, else the first stock of any scan.
    probe = stocks[0]["symbol"] if stocks else next((rows[0]["symbol"] for rows in lists.values() if rows), None)
    snap_date = None
    if probe:
        row = next(r for rows in lists.values() for r in rows if r["symbol"] == probe)
        snap_date = _bar_session(charts[probe]["daily"] if probe in charts else _bars(probe, n=10), row.get("1D"))
    if not snap_date:  # empty scans, or no match: the latest final India session
        row = con.execute("SELECT MAX(snap_date) AS d FROM snapshots WHERE provisional = 0").fetchone()
        snap_date = row["d"] or market_now("in", now).strftime("%Y-%m-%d")

    filters = [SETUP_FILTER[1]]
    if SETUP_ABOVE_EMA:  # last close above the EMA, from the bars (live during market hours, like the chart)
        col, flag = BAR_FIELDS.index(SETUP_ABOVE_EMA), f"above_ema_{SETUP_ABOVE_EMA[3:]}"
        rows = {r["symbol"]: r for r in filt}

        def above(sym):
            last = (charts[sym]["daily"] or [None])[-1]
            if last and last[4] is not None and last[col] is not None:
                return last[4] > last[col]
            return bool(rows[sym].get(flag))  # no bars: the scan row's flag (as of the scan's session)

        keep = {st["symbol"] for st in stocks if above(st["symbol"])}
        for st in setup_list:
            st["symbols"] = [sym for sym in st["symbols"] if sym in keep]
        stocks = [st for st in stocks if st["symbol"] in keep]
        charts = {sym: c for sym, c in charts.items() if sym in keep}
        filters.append(f"above EMA {SETUP_ABOVE_EMA[3:]}")

    live = in_market_hours(now, "in")
    data = {"filter": {"id": SETUP_FILTER[0], "name": SETUP_FILTER[1], "count": len(filt)}, "filters": filters,
            "setups": setup_list, "stocks": stocks}
    digest = _digest({"data": data, "charts": charts, "live": live})
    have = con.execute("SELECT data_hash FROM setups WHERE snap_date = ?", (snap_date,)).fetchone()
    if have and have["data_hash"] == digest:
        return {"stored": False, "date": snap_date, "reason": "Setups already up to date."}
    with con:
        con.execute("UPDATE setups SET charts = NULL WHERE snap_date < ?", (snap_date,))  # charts: latest only
        con.execute("INSERT OR REPLACE INTO setups (snap_date, fetched_at, data_hash, live, data, charts)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (snap_date, datetime.now().isoformat(timespec="seconds"), digest, int(live),
                     json.dumps(data), json.dumps(charts, separators=(",", ":"))))
    return {"stored": True, "date": snap_date, "live": live, "stocks": len(stocks),
            "setups": {st["name"]: len(st["symbols"]) for st in setup_list}}


def setups_due(con, now=None):
    """Whether the setups should be fetched: every time during India's market hours (live prices and bars),
    otherwise when none are stored, the stored ones were fetched live, or they're older than the latest
    stored India session (Trendvue runs its scans after the close)."""
    if in_market_hours(now, "in"):
        return True
    row = con.execute("SELECT snap_date, live FROM setups ORDER BY snap_date DESC LIMIT 1").fetchone()
    if not row or row["live"]:
        return True
    latest = con.execute("SELECT MAX(snap_date) AS d FROM snapshots").fetchone()["d"]
    return bool(latest and row["snap_date"] < latest)


def _setup_key(data):
    """What defines the setups: their scans and filters. Rows stored under another definition don't count toward age."""
    return sorted(st["id"] for st in data.get("setups", [])), data.get("filters")


def _setup_since(con, latest, data):
    """For each stock in the latest setups (stored for session `latest`): (the first session of its current unbroken
    run in the stored setups, how many stored sessions that run has); (latest, 1) = it entered with the latest scans."""
    since = {st["symbol"]: (latest, 1) for st in data["stocks"]}
    running, key = set(since), _setup_key(data)
    for r in con.execute("SELECT snap_date, data FROM setups WHERE snap_date < ? ORDER BY snap_date DESC", (latest,)):
        prev = json.loads(r["data"])
        if not running or _setup_key(prev) != key:
            break
        there = {st["symbol"] for st in prev["stocks"]}
        for sym in list(running):
            if sym in there:
                since[sym] = (r["snap_date"], since[sym][1] + 1)
            else:
                running.discard(sym)
    return since


def setups(con):
    """The latest setups with their charts, for the page; {"date": None} if none are stored.
    Each stock has `since`, the first session of its current run in the setups, and `age`, its sessions in the
    setups from `since` to the latest, both counted (1 = new). Age is counted on the stock's daily bars, so a
    session without a stored fetch still counts."""
    row = con.execute("SELECT * FROM setups ORDER BY snap_date DESC LIMIT 1").fetchone()
    if not row:
        return {"date": None, "setups": [], "stocks": []}
    data = json.loads(row["data"])
    if "setups" not in data:  # stored when there was one setup (scans = [the scan, the filter])
        data = {"filter": data["scans"][-1], "stocks": data["stocks"],
                "setups": [{**data["scans"][0], "symbols": [st["symbol"] for st in data["stocks"]]}]}
    data.setdefault("filters", [data["filter"]["name"]])  # stored before the EMA filter
    charts, latest = json.loads(row["charts"] or "{}"), row["snap_date"]
    since = _setup_since(con, latest, data)
    for st in data["stocks"]:
        first, runs = since[st["symbol"]]
        sessions = sum(1 for b in charts.get(st["symbol"], {}).get("daily") or [] if first <= b[0] <= latest)
        st.update(since=first, age=sessions or runs)
    import triage  # here, not at the top: triage imports this module
    return {"date": latest, "fetched_at": row["fetched_at"], "live": bool(row["live"]),
            **data, "bar_fields": BAR_FIELDS, "charts": charts, "triage": triage.load(con, latest)}


def markets_with_data():
    out = []
    for m, cfg in MARKETS.items():
        if cfg["db"].exists():
            con = connect(m)
            if list_dates(con):
                out.append(m)
            con.close()
    return out


def export_site(out_dir, markets):
    """Write the dashboard as a static site: index.html, data/markets.json, and per market
    data/<market>/index.json (sessions) plus data/<market>/<date>.json per session, holding every
    threshold's numbers, so no server is needed."""
    out, data_root = Path(out_dir), Path(out_dir) / "data"
    if data_root.exists():
        shutil.rmtree(data_root)
    per_metric = ("strength", "industry_strength", "sectors", "industries")
    counts = {}
    for market in markets:
        data_dir = data_root / market
        data_dir.mkdir(parents=True)
        con = connect(market)
        dates = list_dates(con)
        for d in dates:
            by_metric = {m: compare(con, d, m, market=market) for m in METRICS}
            first = by_metric[METRICS[0]]
            doc = {k: v for k, v in first.items() if k not in per_metric + ("weakness", "metric", "dates", "live_dates")}
            doc["by_metric"] = {m: {k: r[k] for k in per_metric} for m, r in by_metric.items()}
            # one stock list at the widest threshold; the page filters it to the selected %
            doc["top_stocks"] = top_stocks(con, d, max(_within(m) for m in METRICS), min_mcap_cr=MARKETS[market]["min_mcap"])
            doc.pop("max_dist_from_high", None)
            (data_dir / f"{d}.json").write_text(json.dumps(doc, separators=(",", ":")))
        (data_dir / "index.json").write_text(json.dumps({
            "dates": dates, "live_dates": live_dates(con), "generated_at": datetime.now().isoformat(timespec="seconds")}))
        if market == "in":  # the Setups page
            setup = setups(con)
            if setup["date"]:
                (data_root / "setups.json").write_text(json.dumps(setup, separators=(",", ":")))
        con.close()
        counts[market] = len(dates)
    (data_root / "markets.json").write_text(json.dumps([{"id": m, "name": MARKETS[m]["name"]} for m in markets]))
    page = (BASE / "static" / "index.html").read_text()
    marker = "<script>window.STATIC_SITE = false;</script>"
    assert marker in page, "static/index.html is missing the STATIC_SITE marker"
    (out / "index.html").write_text(page.replace(marker, "<script>window.STATIC_SITE = true;</script>"))
    return counts


def _github_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in kv.items())


def _markets_arg(arg, default):
    ms = [m for m in (arg or default).split(",") if m]
    bad = [m for m in ms if m not in MARKETS]
    if bad:
        sys.exit(f"Unknown market(s) {bad}; choose from {list(MARKETS)}")
    return ms


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd in ("snapshot", "auto"):
        # snapshot: fetch now; auto: fetch only when due. Markets are independent: one failing doesn't stop the
        # others, and the exit status reports the failure after every market has run.
        changed, failed = False, []
        for market in _markets_arg(arg, "in"):
            if cmd == "auto":
                with closing(connect(market)) as con:
                    due = snapshot_due(con, market=market)
                if not due:
                    print(f"[{market}] Not due: outside market hours, or today's final data is already stored.")
                    continue
            try:
                res = take_market_snapshot(market)
            except Exception as e:
                if cmd == "auto" and isinstance(e, RuntimeError) and "mid-update" in str(e):
                    res = {"stored": False, "reason": str(e)}  # transient: the next scheduled run picks it up
                else:
                    print(f"[{market}] failed: {e}", file=sys.stderr)
                    failed.append(market)
                    continue
            print(f"[{market}]", json.dumps(res))
            changed = changed or res["stored"]
        if "in" in _markets_arg(arg, "in"):  # the Setups page (India scans), independent of India's own fetch
            with closing(connect("in")) as con:
                if cmd == "auto" and not setups_due(con):
                    print("[setups] Not due: already have the latest scans.")
                else:
                    try:
                        res = take_setups(con)
                        print("[setups]", json.dumps(res))
                        changed = changed or res["stored"]
                    except Exception as e:
                        print(f"[setups] failed: {e}", file=sys.stderr)
                        failed.append("setups")
                # Triage notes on the setups (after the close, once per setups session). A failure doesn't fail
                # the run: the notes are extra, and the next run tries again.
                import triage
                if triage.triage_due(con):
                    try:
                        res = triage.take_triage(con)
                        print("[triage]", json.dumps(res))
                        changed = changed or res["stored"]
                    except Exception as e:
                        print(f"[triage] failed: {e}", file=sys.stderr)
        _github_output(changed=str(changed).lower())
        if failed:
            sys.exit(f"Fetch failed for {failed}")
    elif cmd == "setups":
        with closing(connect("in")) as con:
            print(json.dumps(take_setups(con)))
    elif cmd == "triage":  # triage the latest setups now; "force" redoes the stocks already done
        import triage
        with closing(connect("in")) as con:
            print(json.dumps(triage.take_triage(con, force=arg == "force")))
    elif cmd == "next-wait":
        print(next_wait_seconds(_markets_arg(arg, "in")))
    elif cmd == "export":
        out = arg or "site"
        markets = _markets_arg(sys.argv[3] if len(sys.argv) > 3 else None, ",".join(markets_with_data()) or "in")
        print(f"Exported {export_site(out, markets)} sessions to {out}/")
    elif cmd == "compare":
        market = _markets_arg(sys.argv[3] if len(sys.argv) > 3 else None, "in")[0]
        _print_compare(compare(connect(market), metric=arg or DEFAULT_METRIC, market=market))
    else:
        sys.exit(__doc__)
