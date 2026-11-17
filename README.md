# Daily Market Analysis

A local dashboard of NSE **sector and industry breadth**: for every sector and industry, the % of stocks within N% of their
52-week high. It records one snapshot per trading session in SQLite and shows which groups strengthened since the previous session,
which are strongest now, and the stocks driving them. It can also export those stocks as a TradingView watchlist.
During market hours the numbers are **live**, calculated from current prices.

The data comes from [Trendvue Segment Analysis](https://trendvue.in/segment-analysis?groupBy=sector_performance&sectorDrillMode=industries&metric=near_52w_high&p_near_52w_high=near_52w_10).
It has no dependencies beyond the macOS system `python3` (3.9+).

## Run

```bash
python3 server.py                 # http://localhost:8050   (PORT=8051 python3 server.py for another port)
```

Getting data in:

- **Fetch now** (top of the page) records the latest data. It takes about a minute.
- While the server is running, on weekdays it fetches live data every 15 minutes from 09:15 to 15:45 (`LIVE_REFRESH_MIN=5 python3 server.py` to change).
  From 17:00 it fetches every 30 minutes until that day's final data is stored. The page reloads a live session every 2 minutes.
- To record even when the server isn't running, run `./install_schedule.sh`. It installs a launchd job for weekdays at 17:30 and 21:00, logging to `data/snapshot.log`.
  To uninstall: `launchctl unload ~/Library/LaunchAgents/com.dailymarket.snapshot.plist` and delete that file.

From the command line: `python3 market.py snapshot` · `python3 market.py auto` (fetch only if due) ·
`python3 market.py compare [near_52w_10|near_52w_5|…]` · `python3 market.py export site` (static copy of the dashboard)

## Public site (GitHub Pages)

The workflow [.github/workflows/update.yml](.github/workflows/update.yml) runs the dashboard without a server,
for free, as a static site on GitHub Pages:

- **Schedule:** every 15 minutes on weekdays from 08:37 to 00:22 IST. Each run runs `market.py auto`, which fetches only when due,
  using the same rule as the local server: every run during market hours, then from 17:00 until the day's final data is stored.
  Runs that aren't due finish in seconds. GitHub may start scheduled runs 5–30 minutes late.
- **Fetch now:** Actions tab → *Update dashboard* → **Run workflow**. The page hides its own Fetch now button.
- **Database:** `market.db.gz` is stored as a file on a release named `data`. Each run downloads it, adds new data and uploads it again.
  Keeping it out of git avoids repository bloat.
- **Site:** `market.py export site` writes `index.html` plus `data/index.json` and one `data/<date>.json` per session (about 200 KB each,
  holding all five thresholds). The site is rebuilt only when data changed, or on a push to `main`.
- **Keep-alive:** GitHub pauses schedules in public repos after 60 days without activity, so the workflow makes an empty commit
  if the last commit is more than 50 days old.
- Requires a **public** repository: Pages on private repos needs a paid plan, and public repos get unlimited Actions minutes.
  Settings → Pages → Source must be **GitHub Actions**.

## The dashboard

All percentages follow the **2% / 5% / 10% / 15% / 20%** selector ("% of stocks within X% of their 52W high").
The date picker shows any stored session, compared against the session before it. A live session is marked **LIVE hh:mm**
(the time of its last fetch) and "· live" in the picker.

| Card | Ranks sectors/industries by |
|---|---|
| **Top 5 strong sectors today** | Largest gain since the previous session (percentage points; 12.1% → 16.9% = +4.8 pts) |
| **Top 5 strong industries today** | Same, for industries |
| **Top 5 strong sectors** | Highest % right now |
| **Top 5 strong industries** | Same, for industries |
| **All sectors** table | Every sector, sortable. Click a sector to show its industries. |

Click any row in the four cards to see **its stocks**. The list contains every stock within 10% of its 52W high (closest first), with its company name and its
1-day, 1-week and 1-month moves (live during market hours). Each row shows how many stocks are near their high.

### Rules

| Rule | Value | Setting in `market.py` |
|---|---|---|
| **Universe:** only stocks with market cap of at least | ₹1,000 cr | `MIN_MCAP_CR` |
| A sector/industry is ranked in the cards only with at least | 3 stocks in the universe | `MIN_GROUP_STOCKS` |
| Stock lists and TradingView export include stocks within | 10% of their 52W high | `MAX_DIST_FROM_HIGH` |

- The universe rule applies everywhere: to the sector/industry percentages, the "stocks" counts and the stock lists.
  Because of it, the dashboard calculates the percentages itself from stock-level data instead of using Trendvue's own numbers.
  The same calculation without the filter matches Trendvue's numbers exactly.
- The table still lists groups that are too small to rank.
- Restart the server after changing a setting. Stored data never needs re-fetching, because the rules are applied when the dashboard reads the data.

## TradingView export

**Copy for TradingView** and **↓ .txt** export every stock listed in the four cards as a watchlist:

```
###STRONG SECTORS TODAY,###Healthcare (40),NSE:GLAND,NSE:APLLTD,…,###STRONG INDUSTRIES TODAY,…
```

- TradingView sections are only one level deep, so each card becomes a header section followed by one section per sector/industry.
  Section names include the stock count.
- A watchlist can hold each symbol only once. A stock therefore stays in the first section it appears in, and later sections note it,
  e.g. `###Banks (17 - 17 listed above)`.
- Symbols use the stock's exchange (`NSE:`/`BSE:`). `-` and `&` become `_`, as TradingView does (`NSE:BAJAJ_AUTO`, `NSE:M_M`).
- To import, choose the watchlist name ▾ → **Import list…** in TradingView and pick the .txt file. This is the documented route and keeps the sections.
  Pasting into "Add symbol" adds the stocks, but may not keep the sections.

## How data is collected

Each snapshot fetches the following Trendvue endpoints. They are public JSON, rate-limited to **120 requests/min per IP**,
and the limit is shared with any Trendvue tab open in your browser.

| Endpoint | Used for |
|---|---|
| `/api/segment_analysis/sector_performance`, `/industry_performance` | The list of industries, and Trendvue's own breadth numbers (stored raw for final sessions) |
| `/api/segment_analysis/industry_stocks/<industry>` (×58) | Which stocks are in each industry, and each stock's live 1D/1W/1M moves. A sector's stocks = all the stocks in its industries. |
| `/api/screener-data/stocks?fields=market_cap_basic,close,price_52_week_high,…` | Market cap, exchange, last close and 52W high for every stock (one request) |
| `/api/eod/RELIANCE?days=3` | Daily bars (the last one live) that tell which session each source reflects |

- **All-or-nothing:** the stock-list requests are spaced about 1s apart (`REQUEST_GAP`), and a rate-limited request waits as long as Trendvue asks before retrying.
  If any part still fails, nothing is written and the stored data stays as it was.

### Live vs final

Trendvue updates each stock's 1D move live, but its 52W-high numbers (and the screener's close and 52W high) only after the close.
So during market hours the dashboard calculates them itself:

```
live price  = previous close × (1 + live 1D %)
52W high    = max(previous 52W high, live price)      # a new high today
% from high = live price / 52W high − 1
```

- On end-of-day data this formula reproduces Trendvue's own % from high to within 0.01 for every stock.
- A live snapshot is stored for today and marked provisional. Each fetch replaces it.
  After Trendvue's end-of-day update, a fetch replaces it with Trendvue's final numbers.
- The 1D move of the reference stock `RELIANCE` (`PROBE_SYMBOL`), compared with its daily bars, tells which session each source reflects.
  If the sources disagree, which only happens while Trendvue is mid-update, the fetch stops with "try again in a few minutes".
- A live fetch also stores the previous session's final data if that session is missing or only provisional.
- **Live limitation:** today's intraday high isn't available in bulk. A stock that set a new 52W high earlier today and then fell back
  is measured against its old high, so it looks a little closer to its high than it is. The final data corrects this.
- A morning fetch before the open stores the previous session's final data. Re-fetching a session overwrites it.

## Data

The database is `data/market.db`. Set `MARKET_DB=/path/to.db` to use a different file.

| Table | Contents |
|---|---|
| `snapshots` | One row per trading session (`snap_date`, `fetched_at`, `provisional` = 1 for live data) |
| `segment_values` | Trendvue's numbers per sector/industry for all five thresholds, plus the full raw JSON row |
| `stock_values` | Per stock per session: symbol, company, industry, sector, exchange, `pct_from_52w_high`, `change_1d`/`_1w`/`_1m`, `market_cap_cr`, plus the `price` and `high_52w` used |

## Files

- `market.py`: fetching, storage, settings and comparison logic, plus a command-line interface.
- `server.py`: the web server and the automatic live and end-of-day fetches.
- `.github/workflows/update.yml`: the scheduled GitHub Pages build.
- `static/index.html`: the dashboard and the TradingView export.
- `install_schedule.sh`: the optional launchd schedule.
