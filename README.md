# Daily Market Analysis

A dashboard of **sector and industry breadth** for **India (NSE/BSE)** and the **US (NYSE/NASDAQ)**: for every sector and
industry, the % of stocks within N% of their 52-week high. It records one snapshot per trading session in SQLite and shows which groups strengthened since the previous session,
which are strongest now, and the stocks driving them. It can also export those stocks as a TradingView watchlist.
During market hours the numbers are **live**, calculated from current prices.

**Public site:** https://jeswinjoy93.github.io/daily-market-analysis/ (India and US, updated by GitHub Actions, see below).

India's data comes from [Trendvue Segment Analysis](https://trendvue.in/segment-analysis?groupBy=sector_performance&sectorDrillMode=industries&metric=near_52w_high&p_near_52w_high=near_52w_10);
the US data from TradingView's stock screener.
It has no dependencies beyond the macOS system `python3` (3.9+). Optional: `pypdf` lets the Setups triage read
concall transcripts itself, and a DeepSeek API key adds its AI notes (see [Triage notes](#triage-notes-setups-page)).

## Run

```bash
python3 server.py                 # http://localhost:8050   (PORT=8051 python3 server.py for another port)
```

Getting data in:

- To fetch right away: `python3 market.py snapshot` (India, about a minute) or `python3 market.py snapshot us` (US, a second or two);
  or `curl -X POST "localhost:8050/api/snapshot?market=us"` while the server runs.
- While the server is running, it fetches each market on its own weekdays: live data every 5 minutes during market hours
  (India 09:15–15:50 IST, US 09:30–16:05 New York time; `LIVE_REFRESH_MIN=15 python3 server.py` to change), then every 30 minutes
  after the close (India from 17:00 IST, US from 16:20 New York time) until that day's final data is stored.
  The page reloads a live session every 2 minutes.
- To record even when the server isn't running, run `./install_schedule.sh`. It installs a launchd job for weekdays at 17:30 and 21:00, logging to `data/snapshot.log`.
  To uninstall: `launchctl unload ~/Library/LaunchAgents/com.dailymarket.snapshot.plist` and delete that file.

From the command line (`MARKET` = `in` or `us`, default `in`):
`python3 market.py snapshot [in,us]` · `python3 market.py auto [in,us]` (fetch only if due) ·
`python3 market.py compare [near_52w_10|…] [MARKET]` · `python3 market.py export site [in,us]` (static copy of the dashboard; default: markets with data)

## Public site (GitHub Pages)

The workflow [.github/workflows/update.yml](.github/workflows/update.yml) runs the dashboard without a server,
for free, as a static site on GitHub Pages:

- **Schedule:** runs keep themselves going. Each run ends with a job that waits until the next update is due
  (`market.py next-wait in,us`: every 5 minutes while either market is open or past its close on a weekday, otherwise the
  next open) and then starts the next run. Each run runs `market.py auto in,us`, which fetches each market only when due, using
  the same rule as the local server: every run during that market's hours, then after its close until the day's final data is
  stored. A run where nothing is due takes a few seconds.
  The markets are fetched independently: if one fails, the other's new data is still saved and published.
  GitHub's own cron schedule is kept only as a backup that restarts the chain if it ever stops: on a new repo it didn't fire
  at all for hours. Only one waiting job exists at a time (a newer one cancels the older), so the chain can't multiply.
- **Fetch right away:** Actions tab → *Update dashboard* → **Run workflow** (fetches both markets).
- **Databases:** `market.db.gz` (India) and `market_us.db.gz` (US) are stored as files on a release named `data`. Each run
  downloads them, adds new data and uploads them again. Keeping them out of git avoids repository bloat.
- **Site:** `market.py export site` writes `index.html`, `data/markets.json`, and per market `data/<market>/index.json` plus one
  `data/<market>/<date>.json` per session (about 400 KB for India, 650 KB for the US).
  Each session file holds every threshold's numbers and the stocks within 20%; the page keeps those within the selected %.
  The site is rebuilt only when data changed, or on a push to `main`.
- **Keep-alive:** GitHub pauses schedules in public repos after 60 days without activity, so the workflow makes an empty commit
  if the last commit is more than 50 days old.
- Requires a **public** repository: Pages on private repos needs a paid plan, and public repos get unlimited Actions minutes.
  Settings → Pages → Source must be **GitHub Actions**.

## The dashboard

Everything follows the **5% / 10% / 15% / 20%** selector ("% of stocks within X% of their 52W high"): the percentages,
the stock lists, the counts and the TradingView export. Each row shows its count next to the name, e.g. **Textiles (14/42)**:
14 of its 42 stocks are within X% of their high, so 14/42 = the row's %.
The page shows the latest session, compared against the session before it. A live session is marked **LIVE hh:mm**
(the time of its last fetch). Older sessions stay in the database (`python3 market.py compare`, or `/api/compare?date=YYYY-MM-DD`).

An **India / US** switch picks the market, and a **Strong / All** switch picks the view (both remembered between visits): **Strong** shows the four top-5 cards,
**All** shows the two full tables. The TradingView export follows the view (below).

| View | Card / table | Ranks sectors/industries by |
|---|---|---|
| Strong | **Top 5 strong sectors today** | Largest gain since the previous session (percentage points; 12.1% → 16.9% = +4.8 pts) |
| Strong | **Top 5 strong industries today** | Same, for industries |
| Strong | **Top 5 strong sectors** | Highest % right now |
| Strong | **Top 5 strong industries** | Same, for industries |
| All | **All sectors** table | Every sector. Sorted by **Current %** (highest first); click the **Current %** or **Delta** header to sort (again to reverse). Click a sector for its stocks. |
| All | **All industries** table | Every industry, sortable the same way. Click an industry for its stocks. |

Click any row in the cards or the tables to see **its stocks**: every stock within the selected X% of its 52W high (closest first),
with its company name and its 1-day, 1-week and 1-month moves (live during market hours).

Long names are shortened with "…" so the count always fits on the same line (hover for the full name). On phones the delta drops
its "pts" unit to leave more room.

### Rules

| Rule | Value | Setting in `market.py` |
|---|---|---|
| **Universe:** only stocks with market cap of at least | India ₹1,000 cr · US $1B | `MARKETS[...]["min_mcap"]` |
| A sector/industry is ranked in the cards only with at least | 3 stocks in the universe | `MIN_GROUP_STOCKS` |

- The universe rule applies everywhere: to the sector/industry percentages, the (near/total) counts and the stock lists.
  Because of it, the dashboard calculates the percentages itself from stock-level data instead of using Trendvue's own numbers.
  The same calculation without the filter matches Trendvue's numbers exactly.
- The All tables still list groups that are too small to rank.
- Restart the server after changing a setting. Stored data never needs re-fetching, because the rules are applied when the dashboard reads the data.

## Setups page

The **Breadth / Setups** switch at the top opens **Setups** (also at `…/#setups`): the stocks in any of three Trendvue
daily scans that also pass two filters, **each listed once**:

| Scan | Trendvue scan |
|---|---|
| **High Tight Flag** | High Tight Flag Stocks (`high_tight_flags_stocks`) |
| **Near All-Time High** | Stocks Near All-Time Highs (`near_ATH`) |
| **Top Movers** | Top Movers (`top_movers`) |

1. **Tight Consolidation:** the stock is also in Trendvue's Tight Consolidation scan (not Consolidation Breakout).
2. **Above EMA 50:** its last close is above its 50-day EMA, taken from the same daily bars as the chart (so it's live
   during market hours, like the chart's EMA line). Trendvue's own `above_ema_50` flag is used only if a stock has no bars.

Laid out like Trendvue's watchlist, using the full window width (the chart also fills the window's height):

- **Left:** the stocks, newest in the setups first, each with its company name and its **age** in the setups (stocks of
  the same age in Tight Consolidation's order, tightest first). The header shows how many there are. Age is the number
  of sessions since the stock entered the list, counting the entry session and the latest one: **New** on the session
  it entered, then `2d`, `3d`, … Hover for the date it entered.
  A stock that drops out and comes back starts again at New. Age is worked out from the stored sessions (see below), so it
  counts from the first session stored with the current scans and filters: changing `SETUP_SCANS`, `SETUP_FILTER` or
  `SETUP_ABOVE_EMA` starts every stock again at New.
- **Right:** the selected stock's chart: black OHLC bars (open tick left, close tick right) on a log scale with
  EMA 20/50/200 and volume, **Daily** (1M/3M/6M/1Y) or **Weekly** (6M/1Y/3Y/5Y), remembered between visits. Hover for a
  bar's values. Above it: price, 1D move and market cap. The symbol links to its Trendvue page, plus a
  TradingView link.
- **Scans:** after the sector and industry, a tag for each scan the stock is in; Top Movers adds the periods it leads,
  e.g. `Top Movers 1Y`.
- **Strong sectors:** the sector name above the chart is shaded green when it's one of India's 5 strongest sectors
  overall: the Breadth page's *Top 5 strong sectors* card (highest % now) at the selected %, not the day's biggest
  gainers. Hover it for its rank and %.
- **Flip** between stocks with **↑ / ↓** or the **‹ ›** buttons.
- **Copy for TradingView / ↓ .txt** export the list as one section, e.g.
  `###SETUPS - TIGHT CONSOLIDATION + ABOVE EMA 50 (20),NSE:SHAILY,…`.

The lists are Trendvue's as-is: the dashboard's market-cap universe rule doesn't apply here. The scans are run after each
close, so during market hours they're still the previous session's (the subtitle says which) while the prices and the
last bar are live.

**How it's fetched** (`take_setups` in `market.py`): one request per scan (`/api/daily_scans/<scan>/enriched`, four in
all), then daily and weekly bars for each stock in any scan that's also in Tight Consolidation, once per stock
(`/api/eod/<symbol>?days=260[&timeframe=weekly]`, with Trendvue's EMAs; about 20 stocks, so ~40 requests ~1s apart).
The scans carry no date, so they're dated like the India snapshot: a stock's 1D move is matched to its daily bars.
It runs with every India `snapshot`/`auto` (also `python3 market.py setups`, or on its own in the local server's loop),
and is due during India's market hours, then until the scans of the latest stored session are in.
Results are stored per session in the India database's `setups` table (charts for the latest session only; the
earlier sessions' stock lists give each stock's age: `setups()` walks back through them while the stock is still in
them, then counts its daily bars from that session on, so a session with no stored fetch still counts) and
exported as `data/setups.json` (~650 KB, ~240 KB gzipped). A setups failure doesn't stop the India or US fetch.
To change the scans, edit `SETUP_SCANS` / `SETUP_FILTER` (scan ids are in `https://trendvue.in/api/daily_scans`);
`SETUP_ABOVE_EMA` sets the EMA rule (`"ema20"`, `"ema50"`, `"ema200"`, or `None` to turn it off).

## Triage notes (Setups page)

The **Chart | Notes** switch above the chart (or the **N** key; remembered between visits) swaps the chart for each
setup stock's notes, at the same full width: the story on the left, the numbers and sources on the right (one column
on narrow windows). The notes are written once per setups session after India's close by `triage.py`. Code does the numbers; [DeepSeek](https://api-docs.deepseek.com/) (`deepseek-flash`)
does the reading.

| Note | From | How |
|---|---|---|
| **Sector / Industry Strong, Neutral or Weak** (↑/↓ = rising/falling) | This dashboard's own data | Rank on % of stocks within **15%** of their 52W high (₹1,000 cr universe): top third Strong, bottom third Weak. Arrow: a change of 3+ pts over the last 5 sessions. Hover for the numbers, the 1M–1Y returns and % above EMA 200 (Trendvue's sector/industry rows). |
| **What's driving it**: a summary, up to 4 drivers (results, order, capacity, policy, geopolitics, sector, fund raise, corporate, rating) and what to watch | DeepSeek, from the filings and the headlines it picked (below) | Each driver links the filing or headline it came from, or names the sector or industry status when it comes from those numbers; a driver the model can't tie to one is dropped. |
| **Sector and industry story**: short term (2 weeks) and long term (6 months) | DeepSeek, from Google News headlines | Same, with sources. |
| **Quarterly results, ownership, financial health**, growth (Accelerating / Slowing / Mixed / Declining), red flags, next results date | Trendvue (`/api/stock_bundle/<symbol>`) | Computed in code. The results date is Trendvue's estimate, not a confirmed board meeting: marked red within 14 days. |
| **Earnings quality**: Red flag / Check / OK | The stock's screener.in financials (consolidated when reported and up to date), and its other-income breakdown into exceptional items and normal other income | Computed in code, latest quarter vs the previous 4 and the year-ago quarter; yearly for cash. **Red:** an exceptional gain ≥ 10% of pre-tax profit (PBT); other income ≥ 50% of PBT, or ≥ 2× its 4-quarter average; PBT growth ≥ 20 pts ahead of growth without other income, or a core loss behind a reported profit; operating cash flow < 50% of net profit over 3 years, or negative with a profit; debtor days up ≥ 30% and ≥ 15 days over 2 years. **Check:** other income ≥ 30% of PBT; a year-ago exceptional gain; tax < 10% when usually ≥ 20%; operating margin ≥ 5 pts above its 4-quarter average; sales ≥ 1.5× the 4-quarter average and ≥ +50% YoY; cash conversion < 80%. Cash and receivable checks are skipped for lenders. Thresholds at the top of `earnings_quality` in `triage.py`. The AI note is given the results and mentions a red one that bears on the move. |
| **Concall guidance** | The latest concall transcript (a BSE PDF linked on screener.in) | Read with `pypdf` if installed, else DeepSeek's fetch API; summarised by DeepSeek once per transcript. |
| **Latest filings** | The stock's screener.in page (its 5 latest BSE/NSE announcements, with screener's one-line summaries) | |
| **Headlines** | Google News (India), last 14 days (28 if needed); code filters, DeepSeek picks | Searched by the company's name, short name, symbol and apostrophe form. Code keeps up to 36 that name the company in the headline itself, drops templated price, rating and list pages, and keeps one copy of a story. First DeepSeek reads the newest 12 and picks up to 6 worth reading, most important first, leaving out other companies or people with a similar name. If it picks fewer than 6, it is shown the next 12 older ones, up to 2 more times; when the 14 days run out, the search is run once more over 28 days for older headlines. The note is then written from the picks. If picking fails, the note reads the newest 12 and the newest 6 are shown. |

- **Setup:** put a DeepSeek API key in `DEEPSEEK_API_KEY` (locally: `export DEEPSEEK_API_KEY=…` before `python3 server.py`;
  on GitHub: Settings → Secrets and variables → Actions → New repository secret). Without a key everything except
  the DeepSeek notes is still collected and shown. `DEEPSEEK_MODEL` picks another model.
- **When:** after India's close, once the latest setups are final (`market.py auto`, the local server's loop, or
  `python3 market.py triage [force]`). Only stocks without notes are done, so a run costs nothing once a session is done.
  A stock whose notes fail is retried up to 3 times. About 5 minutes for ~20 stocks (the fetches are spaced out).
- **Cost:** about 1,500 tokens per stock or group and ~13,000 per new transcript: roughly US$0.10 the first night
  and US$0.03–0.05 after, at DeepSeek's peak prices. Each run prints its token use and cost estimate.
- **Public:** the notes are exported with the setups (`data/setups.json`), so they're on the public site.
- **Limits:** BSE's and NSE's own APIs block automated requests, so filings come from screener.in, which shows only
  the latest 5 announcements without a login (its "Important" tab and concall summaries need one, and aren't used).
  screener.in's terms may not allow automated use; it's one page per stock per day, spaced 2s apart.
  Pledged shares aren't covered. AI notes can be wrong: check the linked sources before trading.
- Stored in the India database's `triage` table, one row per setups session and stock, sector or industry.

## TradingView export

**Copy for TradingView** and **↓ .txt** export the stocks in the current view, at the selected X%, as a watchlist
(the file is named e.g. `Market Analysis India 2026-11-17 Strong within 10%.txt`):

- **Strong:** every stock listed in the four cards, one header section per card.
  ```
  ###STRONG SECTORS TODAY,###Healthcare (40),NSE:GLAND,NSE:APLLTD,…,###STRONG INDUSTRIES TODAY,…
  ```
- **All:** every sector as a header section (in the table's current sort, sectors with no stocks skipped), followed by its industries'
  stocks. A sector's stocks are exactly its industries' stocks, so this covers every sector and industry without repeating a symbol.
  ```
  ###TEXTILES (14),###Textiles & Apparels (14),NSE:WELSPUNLIV,…,###HEALTHCARE (42),###Healthcare Services (15),…
  ```

- TradingView sections are only one level deep, so each card (or sector) becomes a header section followed by one section per sector/industry.
  Section names include the stock count.
- A watchlist can hold each symbol only once. In the Strong export a stock can appear in several cards, so it stays in the first
  section it appears in, and later sections note it, e.g. `###Banks (17 - 17 listed above)`. The All export has no repeats.
- Symbols use the stock's exchange. India: `NSE:`/`BSE:`, with `-` and `&` turned into `_` as TradingView does (`NSE:BAJAJ_AUTO`, `NSE:M_M`).
  US: TradingView's own tickers, as-is (`NASDAQ:AAPL`, `NYSE:BRK.B`). The file name includes the market, e.g. `Market Analysis US 2026-11-17 Strong within 10%.txt`.
- To import, choose the watchlist name ▾ → **Import list…** in TradingView and pick the .txt file. This is the documented route and keeps the sections.
  Pasting into "Add symbol" adds the stocks, but may not keep the sections.

## US market

`us.py` fetches the US with **one request** to TradingView's screener (`scanner.tradingview.com/america/scan`). It returns every
stock with its sector, industry, price, 52-week high, 1D/1W/1M moves and market cap, in TradingView's own classification
(20 sectors, ~126 industries in the $1B universe).

- **Common stocks only**, listed on **NYSE, NASDAQ and NYSE American (AMEX)**: no preferreds, ADRs (e.g. TSM, ASML), funds,
  units or OTC listings.
- **One share class per company:** GOOGL/GOOG, BRK.A/BRK.B, FOXA/FOX etc. would count twice, so only the most traded class is
  kept. Classes are matched by TradingView's company logo id plus the first two words of the name and a similar market cap,
  which keeps affiliates such as Coca-Cola and its bottlers, or Cheniere Energy and Cheniere Energy Partners, separate.
- **Prices are 15 minutes delayed** (TradingView's free data), and its 52-week high includes today, so the % from the high is
  computed directly: `price / max(52W high, price) − 1`. No end-of-day estimate is needed.
- **Sessions are dated in New York time.** Before 09:30 a fetch belongs to the previous trading day. Data is live (provisional)
  until 16:20, then final. A market holiday is detected when at least 95% of prices are unchanged from the previous session,
  and nothing is stored.
- Stored in `data/market_us.db` (`MARKET_DB_US` to change), same tables as India; `market_cap_cr` holds US$ million there.
- **Terms:** this is TradingView's unofficial screener endpoint, and their terms restrict automated access and republishing
  their data. The public site publishes only derived numbers (% near the high, 1D/1W/1M moves), no prices, but if TradingView
  objects or blocks the requests, remove `us` from the workflow (`market.py … in`) and the page goes back to India only.

## How data is collected (India)

Each India snapshot fetches the following Trendvue endpoints. They are public JSON, rate-limited to **120 requests/min per IP**,
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
- Which session each source reflects is found by matching the reference stock `RELIANCE`'s (`PROBE_SYMBOL`) 1D move in that source
  to the daily bar with the same move. (Matching prices doesn't work: the latest bar's close is derived from the move.) The data
  counts as final only when the listing's and the stock lists' 1D moves agree for at least 90% of stocks.
  If the sources disagree, which only happens while Trendvue is mid-update, the fetch stops with "try again in a few minutes".
- A live fetch also stores the previous session's final data if that session is missing or only provisional.
- **Live limitation:** today's intraday high isn't available in bulk. A stock that set a new 52W high earlier today and then fell back
  is measured against its old high, so it looks a little closer to its high than it is. The final data corrects this.
- A morning fetch before the open stores the previous session's final data. Re-fetching a session overwrites it.

## Data

The databases are `data/market.db` (India) and `data/market_us.db` (US). Set `MARKET_DB` / `MARKET_DB_US` to use different files.

| Table | Contents |
|---|---|
| `snapshots` | One row per trading session (`snap_date`, `fetched_at`, `provisional` = 1 for live data) |
| `segment_values` | India only: Trendvue's numbers per sector/industry for all five thresholds, plus the full raw JSON row |
| `setups` | India only: per session, each setup's scan size and stocks after the filters (JSON), plus their chart bars for the latest session |
| `triage` | India only: per setups session, notes on each setup stock and its sector and industry (facts, filings, headlines, DeepSeek notes) |
| `stock_values` | Per stock per session: symbol, company, industry, sector, exchange, `pct_from_52w_high`, `change_1d`/`_1w`/`_1m`, `market_cap_cr` (₹ crore; US$ million for the US), plus the `price` and `high_52w` used |

## Files

- `market.py`: per-market settings, India fetching, storage, comparison logic and static export, plus a command-line interface.
- `us.py`: the US fetch from TradingView's screener.
- `triage.py`: the Setups triage notes (Trendvue, screener.in, Google News, DeepSeek).
- `server.py`: the web server and the automatic live and end-of-day fetches.
- `.github/workflows/update.yml`: the scheduled GitHub Pages build.
- `static/index.html`: the dashboard (Breadth and Setups pages) and the TradingView export.
- `install_schedule.sh`: the optional launchd schedule.
