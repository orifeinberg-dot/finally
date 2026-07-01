# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations — the GBM simulator and the Massive API client (see `MASSIVE_API.md`) — sit behind one abstract interface, so SSE streaming, portfolio valuation, and trade execution never know or care which one is active.

This describes the interface as actually implemented in `backend/app/market/` (not just a design sketch — this is what's built and tested).

## Design Goals

- **Source-agnostic downstream code**: swapping simulator ↔ Massive is a one-line env var change, zero code changes elsewhere.
- **Push, not pull**: data sources write to a shared cache on their own schedule (500ms for the simulator, 15s for Massive); consumers read the cache, they never call the data source directly.
- **Cheap reads**: SSE streams to the browser every 500ms regardless of source — the cache read must be fast and non-blocking even while Massive's synchronous HTTP call is in flight elsewhere.

## Core Data Model

```python
# app/market/models.py
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        ...
```

`change`, `change_percent`, and `direction` are computed properties, not stored fields — there's only one source of truth (`price` vs `previous_price`), so they can't drift out of sync.

This is the only object type that leaves the market data layer. Massive's richer response (bid/ask, OHLC, volume) is deliberately flattened down to this shape at the boundary — the rest of the app doesn't need to know Massive exists.

## Abstract Interface

```python
# app/market/interface.py
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

Both `SimulatorDataSource` and `MassiveDataSource` implement this. Neither returns prices from these methods — prices only ever flow through the cache.

## Price Cache

The single point of truth both sources write to and everything else reads from.

```python
# app/market/cache.py
from threading import Lock

class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # bumped on every update — cheap SSE change detection

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Computes previous_price from what was cached before."""

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_price(self, ticker: str) -> float | None: ...      # convenience: just the float
    def get_all(self) -> dict[str, PriceUpdate]: ...            # shallow copy, safe to iterate
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...   # SSE polls this instead of diffing dicts every 500ms
```

Guarded by a plain `threading.Lock` rather than an asyncio lock — the Massive source's blocking HTTP call runs in a worker thread via `asyncio.to_thread`, so cache access genuinely crosses threads, not just coroutines.

The `version` counter exists purely so the SSE loop can skip re-serializing and re-sending identical data: it polls `cache.version`, and only calls `get_all()` + JSON-encodes when the version has changed since the last send.

## Factory

Selects the implementation at startup based on `MASSIVE_API_KEY`:

```python
# app/market/factory.py
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

Returns an unstarted source — the caller is responsible for `await source.start(tickers)`.

## Massive Implementation

```python
# app/market/massive_client.py
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

class MassiveDataSource(MarketDataSource):
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call. Free tier: poll every 15s (5 req/min cap).
    """

    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll so the cache isn't empty
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous — run in a thread, don't block the event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms -> seconds
                    )
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — retry on the next interval. Common causes: 401, 429, network.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

Key decisions, and why:

- **One HTTP call per poll, not one per ticker.** `get_snapshot_all(tickers=[...])` returns every requested ticker in a single response — essential for staying under the free tier's 5 req/min limit regardless of watchlist size.
- **`asyncio.to_thread` around the client call.** The `massive` package is a synchronous `requests`-based client; calling it directly from an `async def` would block the event loop (and therefore the SSE stream) for the duration of the HTTP round-trip.
- **Per-snapshot try/except inside the loop.** One malformed or missing field (e.g. `last_trade` is `None` for a delisted/halted ticker) shouldn't drop the other 9 tickers in the batch.
- **Outer try/except around the whole poll, no re-raise.** A failed poll (rate limit, network blip, bad key) just leaves the cache stale until the next interval — it must never kill the background task, or prices freeze permanently.
- **Timestamp conversion.** Massive returns Unix milliseconds; `PriceUpdate.timestamp` is Unix seconds (matching `time.time()`), so this is the one place that conversion has to happen.

## Simulator Implementation

Wraps the `GBMSimulator` (see `MARKET_SIMULATOR.md`) in the same async-loop shape as the Massive source, but on a 500ms interval instead of 15s:

```python
# app/market/simulator.py
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)  # seed cache before first tick
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Both implementations share the same shutdown pattern: `stop()` cancels the background task and awaits it, swallowing `asyncio.CancelledError`, so it's safe to call from FastAPI's shutdown handler without leaking a task.

## Integration with SSE

```python
# app/market/stream.py
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(_generate_events(price_cache, request), media_type="text/event-stream", ...)
    return router

async def _generate_events(price_cache: PriceCache, request: Request, interval: float = 0.5):
    yield "retry: 1000\n\n"
    last_version = -1
    while True:
        if await request.is_disconnected():
            break
        if price_cache.version != last_version:
            last_version = price_cache.version
            data = {ticker: u.to_dict() for ticker, u in price_cache.get_all().items()}
            yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(interval)
```

Note this polls the cache every 500ms regardless of which source is active — even when Massive is only updating the cache every 15s, the SSE loop still checks every 500ms and simply resends nothing new (`version` unchanged) most of the time. The frontend doesn't need to know or care about the difference.

## File Structure (as built)

```
backend/app/market/
  __init__.py         # re-exports the public API
  models.py           # PriceUpdate
  interface.py        # MarketDataSource ABC
  cache.py            # PriceCache
  factory.py           # create_market_data_source()
  massive_client.py    # MassiveDataSource
  simulator.py         # GBMSimulator + SimulatorDataSource (see MARKET_SIMULATOR.md)
  seed_prices.py        # SEED_PRICES, TICKER_PARAMS, correlation constants
  stream.py             # SSE router factory
```

## Lifecycle

1. **App startup**: `cache = PriceCache()`; `source = create_market_data_source(cache)`; `await source.start(initial_tickers)`.
2. **Watchlist changes**: `await source.add_ticker(t)` / `await source.remove_ticker(t)`.
3. **SSE streaming**: reads `cache.get_all()` whenever `cache.version` changes, roughly every 500ms.
4. **Trade execution**: reads the fill price via `cache.get_price(ticker)`.
5. **App shutdown**: `await source.stop()`.

## Public Import Surface

Downstream code (routes, portfolio logic, tests) imports only from the package root, never reaches into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```
