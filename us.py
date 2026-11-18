"""US market: sector/industry breadth from TradingView's stock screener.

One POST to the screener returns every common stock listed on NYSE, NASDAQ and NYSE American (AMEX) with its
sector, industry, price, 52-week high, 1D/1W/1M moves and market cap. TradingView's free data is 15 minutes
delayed and its 52-week high includes today, so the % from the high is computed directly, with no end-of-day wait.

Stored in its own database (market.MARKETS["us"]["db"]) with the same schema as India; market_cap_cr holds
US$ million there. Sessions are dated in New York time.
"""
import re
from datetime import timedelta

import market

SCAN_URL = "https://scanner.tradingview.com/america/scan"
EXCHANGES = ["NYSE", "NASDAQ", "AMEX"]  # AMEX = NYSE American; OTC listings (mostly foreign shares) are left out
COLUMNS = ["name", "description", "exchange", "sector", "industry", "close", "change", "Perf.W", "Perf.1M",
           "price_52_week_high", "market_cap_basic", "Value.Traded", "logoid"]


def fetch_rows():
    body = {
        "markets": ["america"],
        "options": {"lang": "en"},
        "filter": [
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "typespecs", "operation": "has", "right": ["common"]},  # no preferreds, ADRs, funds or units
            {"left": "exchange", "operation": "in_range", "right": EXCHANGES},
        ],
        "columns": COLUMNS,
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 20000],
    }
    data = market.fetch_json(SCAN_URL, body=body)
    rows = [dict(zip(COLUMNS, r["d"])) for r in data.get("data") or []]
    if len(rows) < 1000:
        raise RuntimeError(f"TradingView returned only {len(rows)} US stocks; existing data left unchanged")
    return rows


def _company_key(r):
    """Share classes of one company share TradingView's logo id and start with the same name ("Alphabet Inc.",
    "Berkshire Hathaway Inc." / "... Inc. New"). The logo id alone is too broad (Coca-Cola's bottlers, Blackstone's
    funds share it), so the first two words of the name must match as well."""
    name = (r.get("description") or "").lower().replace("-", "")  # "Coca-Cola" stays one word
    words = re.sub(r"[^a-z0-9 ]", " ", name).split()[:2]
    return (r["logoid"], *words) if r.get("logoid") else ("symbol", r["name"])


def _one_class_per_company(rows):
    """Companies with several share classes (GOOGL/GOOG, BRK.A/BRK.B) would count twice: keep the most traded.
    Same company = same _company_key and similar market cap (classes are valued alike; affiliates that share a
    name, like Cheniere Energy and Cheniere Energy Partners, are not)."""
    kept = {}
    for r in sorted(rows, key=lambda r: -(r.get("Value.Traded") or 0)):
        cap = r.get("market_cap_basic") or 0
        same = kept.setdefault(_company_key(r), [])
        if not any(min(cap, c) >= 0.65 * max(cap, c) > 0 for c in (k.get("market_cap_basic") or 0 for k in same)):
            same.append(r)
    return [r for group in kept.values() for r in group]


def session(now=None):
    """(session date, live) in New York time: today once trading has started (live until the close data is in),
    otherwise the last weekday. Market holidays are caught later by comparing prices with the previous session."""
    t = market.market_now("us", now)
    cfg = market.MARKETS["us"]
    d = t.date()
    if t.weekday() < 5 and (t.hour, t.minute) >= cfg["hours"][0]:
        return d.isoformat(), (t.hour, t.minute) < cfg["final_after"]
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat(), False


def take_snapshot(con, now=None):
    rows = _one_class_per_company(fetch_rows())
    snap_date, live = session(now)

    stocks = []
    for r in rows:
        if not r.get("sector") or not r.get("industry"):
            continue
        close, high = r.get("close"), r.get("price_52_week_high")
        pct = None
        if close and high:
            high = max(high, close)
            pct = round((close / high - 1) * 100, 2)
        mcap = r.get("market_cap_basic")
        stocks.append({"symbol": r["name"], "company": r.get("description"), "industry": r["industry"], "sector": r["sector"],
                       "pct": pct, "change_1d": r.get("change"), "change_1w": r.get("Perf.W"), "change_1m": r.get("Perf.1M"),
                       "mcap": round(mcap / 1e6, 1) if mcap else None, "exchange": r.get("exchange"),
                       "price": close, "high": high})

    # A market holiday (or a fetch before the open) returns the previous session's prices again.
    prev = con.execute("SELECT id, snap_date, provisional FROM snapshots WHERE snap_date < ? ORDER BY snap_date DESC LIMIT 1",
                       (snap_date,)).fetchone()
    if prev:
        old = dict(con.execute("SELECT symbol, price FROM stock_values WHERE snapshot_id = ?", (prev["id"],)).fetchall())
        same = sum(1 for st in stocks if st["symbol"] in old and old[st["symbol"]] == st["price"])
        if same >= 0.95 * len(stocks):
            return {"stored": False, "live": False, "date": snap_date,
                    "reason": f"No US trading on {snap_date} (prices unchanged since {prev['snap_date']})."}

    have = con.execute("SELECT data_hash, provisional FROM snapshots WHERE snap_date = ?", (snap_date,)).fetchone()
    if have and not have["provisional"] and have["data_hash"] == market._digest(stocks):
        return {"stored": False, "live": False, "date": snap_date, "reason": f"Already up to date ({snap_date})."}
    market._store(con, snap_date, live, stocks)
    return {"stored": True, "live": live, "date": snap_date,
            "sectors": len({s["sector"] for s in stocks}), "industries": len({s["industry"] for s in stocks}),
            "stocks": len(stocks)}
