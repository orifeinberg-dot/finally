# Massive API Reference (formerly Polygon.io)

Research reference for the Massive REST API, focused on what FinAlly needs: current prices and end-of-day prices for a batch of tickers. Verified against the official `massive-com/client-python` repository and `massive.com/docs` (July 2026).

## Background

Polygon.io rebranded to **Massive** on 2025-10-30. The rebrand is cosmetic only — no breaking API changes:

| | Old | New |
|---|---|---|
| PyPI package | `polygon-api-client` | `massive` |
| Import | `from polygon import RESTClient` | `from massive import RESTClient` |
| Base URL | `api.polygon.io` (still works) | `api.massive.com` |
| Endpoints, request/response shapes, auth | unchanged | unchanged |

Existing Polygon API keys work unchanged against the new client and base URL.

## Install

```bash
uv add massive
# or: pip install -U massive
```

Python 3.9+.

## Auth

```python
from massive import RESTClient

client = RESTClient(api_key="your_key_here")
```

`RESTClient()` with no arguments does **not** reliably auto-read an env var (`MASSIVE_API_KEY`/`POLYGON_API_KEY` are naming conventions, not something the library enforces) — always pass `api_key` explicitly. FinAlly reads `MASSIVE_API_KEY` itself and passes it in.

The client is **synchronous**. In an async app (FastAPI), call it via `asyncio.to_thread(...)` so it doesn't block the event loop.

## Plans & Rate Limits

| Plan | Price | Data recency | REST rate limit | WebSocket |
|------|-------|--------------|------------------|-----------|
| Free | $0 | End-of-day / delayed | 5 requests/min | No |
| Starter | $29/mo | 15-min delayed | Unlimited | No |
| Developer | $79/mo | Real-time | Unlimited | No |
| Advanced | $199/mo | Real-time | Unlimited | Yes |

Verify current tiers/pricing at `massive.com/pricing` before relying on exact numbers — they're a business detail that changes independently of the API itself.

**Implication for FinAlly**: on the free tier, "real-time" REST snapshots are actually end-of-day or 15-minute-delayed quotes, not live ticks. The 500ms/tick illusion of a live terminal is a simulator-only feature; polling Massive on the free tier only makes sense at long intervals (matches the 5 req/min cap). True intraday streaming needs a paid plan and the WebSocket client.

## REST Endpoints Used by FinAlly

### 1. Full Market Snapshot — multiple tickers in one call (primary endpoint)

The one call that answers "give me current prices for my whole watchlist."

**HTTP**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

**Python**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.todays_change_percent)
```

**`TickerSnapshot` fields** (verified against the client's model, not guessed from the JSON docs — field names differ between the raw JSON and the Python objects):

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | |
| `day` | `Agg \| None` | today's running bar: `open/high/low/close/volume/vwap/timestamp/transactions` |
| `prev_day` | `Agg \| None` | **previous session's** full bar — this is where "previous close" lives (`prev_day.close`), not on `day` |
| `min` | `MinuteSnapshot \| None` | most recent minute bar |
| `last_trade` | `LastTrade \| None` | `price`, `size`, `exchange`, `timestamp` (Unix **ms**) |
| `last_quote` | `LastQuote \| None` | `bid_price`, `ask_price`, `bid_size`, `ask_size`, `timestamp` |
| `todays_change` | `float \| None` | absolute change vs. previous close |
| `todays_change_percent` | `float \| None` | percent change vs. previous close |
| `fair_market_value` | `float \| None` | Business-plan only |
| `updated` | `int \| None` | last update timestamp |

Note: the raw JSON response uses `prevDay` and `todaysChangePerc`; the Python client renames these to `prev_day` / `todays_change_percent` (snake_case) when it deserializes into `TickerSnapshot`. Use the Python attribute names when writing code against the client, not the JSON field names.

**Query params**: `tickers` (comma-separated, case-sensitive; omit for the entire market), `include_otc` (bool, default `False`).

### 2. Single Ticker Snapshot

For a detail view of one selected ticker.

```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(snapshot.last_trade.price, snapshot.day.high, snapshot.day.low)
```

**HTTP**: `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`

### 3. Previous Close — one ticker's end-of-day bar

```python
agg = client.get_previous_close_agg(ticker="AAPL")
print(agg.open, agg.high, agg.low, agg.close, agg.volume, agg.vwap)
```

**HTTP**: `GET /v2/aggs/ticker/{ticker}/prev` — returns a single `Agg`, not a list.

### 4. Grouped Daily — end-of-day bars for the *entire market* in one call

Useful if FinAlly ever needs EOD prices for many tickers without looping — one request returns every ticker's daily bar for a given date, filter to the watchlist client-side.

```python
aggs = client.get_grouped_daily_aggs(date="2026-06-30", adjusted=True)
by_ticker = {a.ticker: a for a in aggs}
```

**HTTP**: `GET /v2/aggs/grouped/locale/us/market/stocks/{date}`

For a small watchlist (10-50 tickers), `get_snapshot_all(tickers=[...])` is usually the better choice — it's ticker-filtered server-side instead of returning the whole market. `get_grouped_daily_aggs` is worth knowing about but not what FinAlly should use by default.

### 5. Aggregates (Historical Bars)

For charting, not live polling.

```python
aggs = []
for a in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",       # "minute", "hour", "day", "week", "month", ...
    from_="2026-01-01",
    to="2026-06-30",
    limit=50000,
):
    aggs.append(a)   # each a: Agg(open, high, low, close, volume, vwap, timestamp, transactions)
```

`list_aggs` paginates automatically (generator). `get_aggs` returns a materialized `list[Agg]` instead. Both hit `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`.

### 6. Daily Open/Close

```python
oc = client.get_daily_open_close_agg(ticker="AAPL", date="2026-06-30")
print(oc.open, oc.close, oc.high, oc.low, oc.volume)
```

**HTTP**: `GET /v1/open-close/{ticker}/{date}`

### 7. Last Trade / Last Quote

```python
trade = client.get_last_trade(ticker="AAPL")   # .price, .size, .exchange, .timestamp
quote = client.get_last_quote(ticker="AAPL")   # .bid_price, .ask_price, .bid_size, .ask_size
```

## WebSocket (real-time streaming, paid plans only)

```python
from massive import WebSocketClient

ws = WebSocketClient(api_key=api_key, subscriptions=["T.AAPL", "Q.AAPL", "AM.AAPL"])

def handle_msg(messages):
    for m in messages:
        print(m)

ws.run(handle_msg=handle_msg)
```

Channel prefixes: `T.` (trades), `Q.` (quotes/NBBO), `AM.` (per-minute aggregate bars), `A.` (per-second aggregate bars). Use `*` for all tickers or a comma-separated ticker list. Requires a plan that includes WebSocket access (Advanced tier or above) — not usable on Free/Starter/Developer. FinAlly does not use this; it polls REST instead (see `MARKET_INTERFACE.md`), which works on every plan including Free.

## Error Handling

| Status | Meaning |
|---|---|
| 401 | Invalid API key |
| 403 | Endpoint not included in current plan |
| 429 | Rate limit exceeded (Free tier: 5 req/min) |
| 5xx | Server error — client retries a few times by default |

Wrap polling calls in `try/except` and log-and-continue rather than crashing the poll loop; a single failed poll shouldn't kill the background task (see `_poll_once` in `MARKET_INTERFACE.md`).

## Notes for FinAlly

- `get_snapshot_all(tickers=[...])` is the one call that covers "real-time and end-of-day prices for multiple tickers" for a watchlist-sized ticker set — `last_trade.price` for current price, `prev_day.close` for the prior close, `todays_change_percent` for day change. No per-ticker looping needed.
- `last_trade.timestamp` (and other Massive timestamps) are Unix **milliseconds** — divide by 1000 before treating them as `time.time()`-style Unix seconds.
- On Free/Starter, `last_trade.price` may be end-of-day or 15-minute-delayed rather than live — accurate for a demo, not for real trading decisions.
- The snapshot endpoint's `day` object resets at market open; outside market hours it may reflect the previous session until the next open.
