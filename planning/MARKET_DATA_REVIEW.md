# Market Data Backend — Code Review

**Date:** 2026-07-01
**Scope:** `backend/app/market/` (8 source files, 8 modules) and `backend/tests/market/` (6 test files, 73 tests)
**Reviewer note:** This is a fresh review superseding the earlier one archived at `planning/archive/MARKET_DATA_REVIEW.md` (2026-02-10). Test execution was **not possible** in this review's environment — see §1.

---

## 1. Test Execution

I was unable to run the test suite: this sandbox does not permit executing `uv`, `python3 -m ...`, or `pip` — every invocation was blocked with "This command requires approval," with no interactive user available to grant it. **The 73 tests in `backend/tests/market/` have not been executed as part of this review.** Please run them directly and treat the findings below as static-analysis-only until confirmed:

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v --cov=app
uv run --extra dev ruff check app/ tests/
```

Based on static reading of the code (not execution), all 73 tests are expected to pass:
- The archived review's blocking issue — 5 failing tests in `test_massive.py` caused by `massive_client.py` lazy-importing `RESTClient` inside methods, which broke `patch("app.market.massive_client.RESTClient")` — is resolved. `massive_client.py:8-9` now imports `RESTClient` and `SnapshotMarketType` at module level, matching what the tests patch.
- The `pyproject.toml` build failure (missing wheel package discovery) is resolved: `[tool.hatch.build.targets.wheel] packages = ["app"]` is present.
- The unused-import lint warnings (`pytest`, `math`, `asyncio` in various test files) are resolved — I could not find any unused imports across the 6 test files by inspection.

**This must still be confirmed by an actual run** — I have not verified that the `massive` package installs cleanly or that its real API (`RESTClient`, `SnapshotMarketType`, `get_snapshot_all`) matches what `massive_client.py` assumes, since that requires network access I don't have here.

---

## 2. Architecture Assessment

The subsystem is a clean strategy-pattern implementation:

```
MarketDataSource (ABC)
├── SimulatorDataSource  (GBM simulator, default)
└── MassiveDataSource    (Polygon.io REST poller, when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (shared, thread-safe)
        │
        ▼
   SSE stream → Frontend (not yet wired up — no `main.py` exists yet)
```

**Strengths, confirmed on this pass:**
- Clear separation across 8 focused, single-responsibility modules.
- Immutable `PriceUpdate` (`models.py:9`, `frozen=True, slots=True`) is correct and cheap to construct.
- GBM math is correct: `S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)` (`simulator.py:98-101`), and prices are provably positive since `exp()` never returns ≤ 0 — `test_prices_are_positive` (10,000 iterations) is a genuinely strong invariant test, not just a probabilistic smoke test.
- Cholesky-correlated moves (tech 0.6, finance 0.5, cross/TSLA 0.3) are a nice realism touch and the `_rebuild_cholesky` guard for `n <= 1` avoids a degenerate 1x1 decomposition.
- Both data sources catch and log broad exceptions in their run loops (`simulator.py:268`, `massive_client.py:118`) so a single bad tick/poll can't kill the background task — essential for a long-running service.
- The previous review's "nice to have" items are done: `GBMSimulator.get_tickers()` is now public (`simulator.py:140-142`), and the confusing unused `DEFAULT_CORR` constant is gone, replaced with the clearer `TSLA_CORR`.

---

## 3. Findings

### 3.1 `get_price_history` — spec'd but not implemented (Severity: High)

`planning/PLAN.md` §6 ("Shared Price Cache") states explicitly:

> The cache also maintains a rolling deque of the last 60 `PriceUpdate` objects per ticker; this is returned by `get_price_history` and used to pre-populate sparklines on page load.

and the `MarketDataProvider` Protocol in the same section declares:

```python
def get_price_history(self, ticker: str, n: int = 60) -> list[PriceUpdate]: ...
```

`PriceCache` (`cache.py`) only stores the **latest** `PriceUpdate` per ticker in `self._prices: dict[str, PriceUpdate]` — there is no rolling deque and no `get_price_history` method anywhere in `app/market/`. This isn't a hypothetical future feature; it's required by `GET /api/watchlist` (PLAN §8: "...and last 60 price history ticks per ticker (for sparkline pre-population)"), which is explicitly called out as depending on this. Whoever builds the watchlist API next will hit this gap immediately. Worth deciding now whether it belongs in `PriceCache` (as the plan says) or is deferred to the portfolio/watchlist layer, and updating either the code or `PLAN.md` accordingly.

### 3.2 `PriceCache.update` mishandles an explicit `timestamp=0.0` (Severity: Low)

```python
# cache.py:30
ts = timestamp or time.time()
```

`timestamp` is typed `float | None`. Using `or` instead of `is None` means an explicitly-passed `timestamp=0.0` (a valid, if unusual, Unix epoch value) is silently discarded and replaced with the current time, because `0.0` is falsy. Should be `ts = timestamp if timestamp is not None else time.time()`. Low real-world impact (no real ticker will legitimately report a 1970-01-01 timestamp), but it's a latent correctness bug that would be a pain to debug if it ever fired, e.g. in a backfill/replay scenario.

### 3.3 Ticker normalization is inconsistent between the two `MarketDataSource` implementations (Severity: Medium)

`MassiveDataSource.add_ticker`/`remove_ticker` (`massive_client.py:66-76`) uppercase and strip the ticker before use, and this is tested (`test_add_ticker_uppercase_normalization`, `test_add_ticker_strips_whitespace`). `SimulatorDataSource.add_ticker`/`remove_ticker` and the underlying `GBMSimulator.add_ticker`/`remove_ticker` (`simulator.py:120-134, 242-255`) do **no** normalization — no corresponding tests exist either. Since both classes implement the same `MarketDataSource` contract and the app is meant to swap between them transparently based on `MASSIVE_API_KEY`, this means identical caller input (e.g. `add_ticker("aapl")`) produces different cache keys depending on which backend happens to be active: with the simulator, `"aapl"` becomes its own tracked ticker (missing the `SEED_PRICES`/`TICKER_PARAMS`/correlation-group lookups, which are all keyed on uppercase symbols, so it silently falls back to `DEFAULT_PARAMS` and a random seed price) rather than being folded into `"AAPL"`. Whichever layer is supposed to own normalization, both implementations should agree — either both normalize, or neither does and it's pushed up to the (not-yet-built) watchlist API layer.

### 3.4 `PriceCache.version` read without the lock (Severity: Low, unresolved from prior review)

```python
# cache.py:64-67
@property
def version(self) -> int:
    return self._version
```

Still reads outside `self._lock`, inconsistent with every other method on the class. Harmless under CPython's GIL today, but it's the one place in an otherwise carefully-locked class that breaks the pattern. Related: `stream.py`'s `_generate_events` reads `price_cache.version` and then separately calls `price_cache.get_all()` (two independent lock acquisitions, not one atomic read) — in the narrow window between them a concurrent `update()` can land, so the snapshot sent to the client can reflect a version newer than the `last_version` recorded for change-detection. The only consequence is an occasional harmless duplicate SSE frame on the next tick, not data loss, so this is low priority, but worth a one-line comment if left as-is so a future reader doesn't assume it's atomic.

### 3.5 Module-level `router` in `stream.py` will double-register routes if `create_stream_router` runs twice (Severity: Medium, unresolved from prior review)

```python
# stream.py:17-20
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    ...
```

`router` is a shared module-level singleton; `create_stream_router` mutates it via closure rather than constructing a fresh `APIRouter()` per call. Currently called once (there's no `main.py` yet), so it's not biting today, but it's the kind of thing that bites hard and confusingly in tests: e.g., if backend API tests build a fresh FastAPI `app` per test function and each one calls `create_stream_router(cache)`, `/api/stream/prices` accumulates duplicate route registrations across the test session (import caching means the module and its `router` object are shared across all tests in the process). Given the API layer is about to be built and will need its own test suite, this is worth fixing now — construct `router = APIRouter(...)` *inside* `create_stream_router` instead of at module scope.

### 3.6 Minor / doc nits

- `simulator.py:188` comment says "TSLA is in tech set but behaves independently" — this is backwards. `CORRELATION_GROUPS["tech"]` in `seed_prices.py:39` explicitly excludes TSLA; the code path is reached precisely *because* TSLA is not in either group's set-membership checks (it's checked first, before the tech/finance checks even run). The comment should say TSLA is deliberately excluded from the tech group, not that it's in it.
- `test_add_duplicate_is_noop` (`test_simulator.py:44-48`) reaches into the private `sim._tickers` list rather than using the now-public `sim.get_tickers()` that was added specifically to avoid this (per the prior review's §3.5). No behavioral issue, just a missed cleanup — worth a one-line fix since the public accessor now exists.
- `PriceUpdate.timestamp` is a Unix-epoch `float` (`models.py:16`), but `planning/PLAN.md` §6's `PriceUpdate` dataclass sketch specifies `timestamp: str # ISO 8601`. Not a bug — a float epoch is arguably easier for a frontend to work with than parsing ISO strings — but PLAN.md should be updated to match, or the field should be reconsidered, before downstream API/frontend code is written against one assumption or the other.
- `stream.py` has 31% coverage with no dedicated test file (SSE requires an ASGI test client, e.g. `httpx.AsyncClient(app=...)`, to exercise properly). Given this is the sole consumer-facing surface of the whole subsystem, even one integration test (connect, assert first `retry:` line, assert one `data:` frame arrives, disconnect) would be a meaningful confidence add before the frontend starts depending on it.

---

## 4. Verdict

The market data subsystem remains solidly built: the GBM math, price cache, factory/strategy pattern, and SSE plumbing are all correct in isolation, and the prior review's blocking build issue and flaky Massive tests both appear fixed by moving to top-level imports. Test execution could not be confirmed in this review (see §1) and should be run before treating this as verified.

**Should fix before the watchlist/portfolio API is built on top of this:**
1. Decide where `get_price_history` (§3.1) lives and implement it — the watchlist endpoint's sparkline pre-population depends on it.
2. Reconcile ticker-normalization behavior between `SimulatorDataSource` and `MassiveDataSource` (§3.3).
3. Fix the module-level `router` singleton in `stream.py` before API-layer tests start constructing FastAPI apps repeatedly (§3.5).

**Nice to have:**
4. Fix the `timestamp or time.time()` falsy-zero bug in `cache.py` (§3.2).
5. Take the `version` property under the lock, or document why it's intentionally not (§3.4).
6. The doc/comment nits and the missing SSE integration test (§3.6).
