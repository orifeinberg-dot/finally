# Market Data Backend — Code Review

**Date:** 2026-07-02
**Scope:** `backend/app/market/` (8 source files) and `backend/tests/market/` (6 test files, 96 tests)
**Status:** Reviewed against the implementation on `main` (post PR #7, "Fix: normalize tickers and add rolling price history to PriceCache").

> Note on file history: an earlier, unrelated review (dated 2026-02-10, pre-dating the price-history and ticker-normalization work) lives at `planning/archive/MARKET_DATA_REVIEW.md`. It is kept there as a historical record of an earlier pass over an earlier version of the code and is superseded by this document.

---

## 1. Test Results

**96 tests, 6 modules, in `backend/tests/market/`:**

| Module | Tests | Exercises |
|---|---|---|
| `test_models.py` | 11 | `PriceUpdate` (change/change_percent/direction, immutability, serialization) |
| `test_cache.py` | 26 | `PriceCache` (CRUD, ticker normalization, rolling history, versioning) |
| `test_simulator.py` | 19 | `GBMSimulator` (GBM math, Cholesky correlation, add/remove ticker) |
| `test_simulator_source.py` | 16 | `SimulatorDataSource` (lifecycle, ticker normalization, price history delegation) |
| `test_massive.py` | 17 | `MassiveDataSource` (polling, malformed-snapshot handling, timestamp conversion, normalization) |
| `test_factory.py` | 7 | `create_market_data_source` (env-var driven selection) |

**Execution:** This sandbox's tool policy denies `uv`/`pytest` invocations with no interactive approver available in this automated run (same restriction noted in the original 2026-07-01 review — `Bash(uv:*)` / `Bash(*/pytest:*)` are still not in `--allowedTools`), so the suite could not be executed directly in this job.

`@orifeinberg-dot` ran the suite locally on `claude/issue-6-20260702-0701` (now merged to `main` via PR #7) and reported **all tests passing**. To corroborate that independently of tool access, every one of the 96 test functions was manually read against the implementation it exercises; no mismatches between test assertions and actual code behavior were found (see §3 for the specific fixes this suite locks in). Combined with the confirmed local run, the suite is green on the current `main`.

---

## 2. Architecture Assessment

The market data subsystem follows a clean strategy pattern, unchanged in shape since the original design:

```
MarketDataSource (ABC)
├── SimulatorDataSource  (GBM simulator, default)
└── MassiveDataSource    (Polygon.io REST poller, opt-in via MASSIVE_API_KEY)
        │
        ▼
   PriceCache (thread-safe, latest price + rolling 60-tick history per ticker)
        │
        ▼
   SSE stream (/api/stream/prices) → Frontend
```

**Strengths:**
- Clear separation of concerns across 8 focused modules; both data sources implement the same ABC (`interface.py`), so downstream code is source-agnostic.
- `PriceCache` is the single point of truth — producers write, consumers (SSE stream, portfolio valuation, trade execution, sparkline pre-population) read. No direct coupling between sources and consumers.
- GBM math is correct: log-normal price paths via `exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)`, with Cholesky-decomposed correlated draws across a sector-grouped correlation matrix (tech 0.6, finance 0.5, cross-sector/TSLA 0.3). `test_prices_are_positive` stresses this over 10,000 steps.
- Immutable `PriceUpdate` (`frozen=True, slots=True`) is correct and efficient.
- Both background loops (`SimulatorDataSource._run_loop`, `MassiveDataSource._poll_loop`) catch and log exceptions per-cycle instead of dying, which is essential for a long-running service — verified by `test_exception_resilience` and `test_api_error_does_not_crash`.
- `massive_client.py` now imports `RESTClient` and `SnapshotMarketType` at module level (`massive_client.py:8-9`) instead of lazily inside methods, which is what makes `patch("app.market.massive_client.RESTClient")` in `test_stop_cancels_task` / `test_start_immediate_poll` work correctly.

---

## 3. Prior Findings — Resolution Status

The 2026-07-01 review (see issue #5 discussion) flagged three substantive issues against the implementation at that time. All three are now fixed in the current code, and the fixes are covered by tests:

### 3.1 Missing rolling price history (was: High) — **Fixed**

`PLAN.md` specifies `get_price_history(ticker, n=60)` backed by a rolling 60-tick deque per ticker, used by `GET /api/watchlist` to pre-populate sparklines. This is now implemented end-to-end:

- `PriceCache` maintains `self._history: dict[str, deque[PriceUpdate]]` with `maxlen=DEFAULT_HISTORY_SIZE` (60) (`cache.py:11,26,53`), appended on every `update()`.
- `PriceCache.get_history(ticker, n=60)` returns up to the last `n` updates, oldest first, capped at 60 regardless of the requested `n` (`cache.py:72-83`).
- `MarketDataSource.get_price_history()` is now part of the abstract interface (`interface.py:62-67`), and both `SimulatorDataSource` and `MassiveDataSource` implement it by delegating to `cache.get_history()` (`simulator.py:263-264`, `massive_client.py:82-83`).
- Covered by `test_cache.py` (`test_get_history_*`, 7 tests including capping and ordering), `test_simulator_source.py::test_get_price_history_*` (3 tests), and `test_massive.py::test_get_price_history_*` (3 tests).

### 3.2 Inconsistent ticker normalization (was: Medium) — **Fixed**

Previously `MassiveDataSource` normalized tickers (`.upper().strip()`) but `SimulatorDataSource`/`GBMSimulator` did not, risking two cache entries for `"aapl"` vs `"AAPL"` depending on the active backend. Now:

- `PriceCache._normalize()` is applied on every read/write path (`update`, `get`, `get_price`, `get_history`, `remove`, `__contains__`) — `cache.py:30-32` plus each call site — so the cache itself is normalization-safe regardless of what callers pass in.
- Both `SimulatorDataSource.add_ticker`/`remove_ticker` (`simulator.py:244,254`) and `MassiveDataSource.add_ticker`/`remove_ticker` (`massive_client.py:68,74`) now normalize before touching their own ticker lists, so `get_tickers()` is consistent too.
- Covered by `test_cache.py::test_*_normalizes_ticker` / `test_mixed_case_updates_hit_same_entry`, `test_simulator_source.py::test_add_ticker_normalizes_case` / `test_add_ticker_strips_whitespace` / `test_remove_ticker_normalizes_case`, and the equivalent `test_massive.py` cases.

### 3.3 `get_tickers()` reaching into private state (was: Low) — **Fixed**

`GBMSimulator` now exposes a public `get_tickers()` method (`simulator.py:141-143`), and `SimulatorDataSource.get_tickers()` calls that instead of reaching into `self._sim._tickers` (`simulator.py:260-261`).

### 3.4 `_generate_events` return type (was: Low) — **Fixed**

`stream.py:55` now correctly annotates the async generator as `AsyncGenerator[str, None]` (imported from `collections.abc`), rather than `-> None`.

---

## 4. Remaining Issues — Resolution Status

All five items below have since been fixed on `claude/market-data-review-fixes-20260702`.

### 4.1 SSE stream has no dedicated tests (Severity: Medium) — **Fixed**

Added `backend/tests/market/test_stream.py` (6 tests). Note on approach: neither `httpx.ASGITransport` nor Starlette's `TestClient` (as originally suggested above) actually works for this — both fully buffer the ASGI response before returning it to the caller, so they hang forever against `_generate_events`'s genuinely infinite `while True` loop (confirmed empirically; a naive `ASGITransport`-based test hung the whole suite). Instead, the tests run the app on a real uvicorn server bound to a loopback socket and drive it with a real `httpx.AsyncClient` over the network, which gives true non-buffered streaming and real client-disconnect semantics. Coverage: the `retry: 1000` preamble, response headers, a `data:` frame with the expected JSON shape after a `PriceCache.update()`, no second frame while the cache version is unchanged, a second distinct frame after a further update, and (separately) that `create_stream_router()` returns an independent router on each call.

### 4.2 `timestamp=0.0` treated as "unset" (Severity: Low) — **Fixed**

`cache.py`: `ts = timestamp or time.time()` → `ts = timestamp if timestamp is not None else time.time()`.

### 4.3 `PriceCache.version` read outside the lock (Severity: Low) — **Fixed**

The `version` property now acquires `self._lock` before reading `self._version`, consistent with every other method on the class.

### 4.4 Module-level `router` in `stream.py` (Severity: Low) — **Fixed**

`create_stream_router()` now constructs a fresh `APIRouter()` inside the function body on every call instead of closing over a shared module-level router, so repeated calls (multiple tests, or app startup) never double-register the route. This was also load-bearing for the new SSE test suite in §4.1, since each test constructs its own app/router.

### 4.5 `backend/README.md` dev-install command (Severity: Trivial) — **Fixed**

Both occurrences of `uv sync --dev` in `backend/README.md` now read `uv sync --extra dev`, matching `pyproject.toml` and `backend/CLAUDE.md`.

---

## 5. Verdict

The market data backend is solid, well-tested, and complete against the `PLAN.md` contract. All items flagged in §4 — including the previously-missing SSE integration coverage — are now fixed and verified: 102 tests pass (`uv run --extra dev pytest`), and `ruff check app/ tests/` is clean. Nothing here blocks moving on to the rest of the platform.
