# Market Data Backend — Code Review

**Date:** 2026-07-01
**Reviewer:** Claude (automated review, triggered from issue #5)
**Scope:** `backend/app/market/` (8 source files, ~500 lines) and `backend/tests/market/` (6 test files, 71 tests)

---

## 0. Important Caveat: Tests Were Not Executed This Session

This review is **static** — the sandboxed environment this review ran in does not allow execution of `uv`, `pytest`, `python3 -m ...`, or similar commands (every non-trivial `Bash` invocation, including `uv sync`, `python3 -c "import numpy"`, and `command -v uv`, was auto-denied with "This command requires approval," while `git`, `ls`, and `python3 --version` were pre-approved and worked). There was no interactive user available during this automated run to grant the additional permissions.

Everything below on test *correctness* is based on careful manual reading of `backend/tests/market/*.py` against the implementation, not an actual test run. **This is a gap the repo owner should close** — re-run this review (or just `cd backend && uv run --extra dev pytest -v --cov=app`) locally or in a workflow with broader `--allowedTools`, and treat the conclusions below as "should pass on inspection" rather than "verified passing."

An archived review at `planning/archive/MARKET_DATA_REVIEW.md` (dated 2026-02-10) *did* execute the suite at that time and reported 73 tests / 68 passing / 5 failing (all in `test_massive.py`, due to a lazy `massive` import). The current code shows that root cause has since been fixed (see §1), so those 5 failures should no longer reproduce — but this is inferred from the diff, not confirmed by a run.

---

## 1. Test Suite — Static Assessment

**71 test functions** across 6 files in `backend/tests/market/`:

| File | Tests | What it covers |
|---|---|---|
| `test_models.py` | 11 | `PriceUpdate` computed properties, immutability, `to_dict()` |
| `test_cache.py` | 13 | `PriceCache` update/get/get_all/remove/version/contains/len |
| `test_simulator.py` | 20 | GBM math, ticker add/remove, Cholesky rebuild, correlation table, rounding |
| `test_simulator_source.py` | 10 | `SimulatorDataSource` async lifecycle (start/stop/add/remove, background loop) |
| `test_factory.py` | 7 | env-var-driven source selection |
| `test_massive.py` | 13 | `MassiveDataSource` polling, malformed-snapshot handling, error resilience |

All tests read as internally consistent with the current implementation. Notably, **the specific failure mode from the prior review is resolved**: `massive_client.py:8` now imports `from massive import RESTClient` at module level (previously lazy-imported inside `start()`), so `patch("app.market.massive_client.RESTClient")` in `test_stop_cancels_task` / `test_start_immediate_poll` (`test_massive.py:175`, `:194`) now targets a name that actually exists in the module namespace. This should fix all 5 previously-failing tests, contingent on the `massive` package actually being installed via `uv sync` (it's a hard dependency in `pyproject.toml`, so this should hold).

One coverage gap stands out: **there is no `test_stream.py`.** `stream.py` — the SSE endpoint that is the actual external contract consumed by the frontend — has zero dedicated tests. The archived review flagged this at 31% coverage; it appears no test was added since. Testing it requires an ASGI test client (`httpx.AsyncClient(app=...)` or FastAPI's `TestClient` with `stream=True`), which is more setup than the pure-function/mock tests elsewhere, but even one happy-path test (connect, get one `data:` event, disconnect) would catch regressions in the JSON payload shape or the version-diffing logic — the part of this subsystem the frontend actually depends on.

`pyproject.toml` already has the `[tool.hatch.build.targets.wheel] packages = ["app"]` fix that the archived review called "must fix before proceeding" (§3.1 there) — confirmed present at `backend/pyproject.toml:24-25`. Good — `uv sync` / Docker builds should not hit that failure anymore.

---

## 2. Architecture Assessment

The subsystem is a clean strategy-pattern implementation:

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBMSimulator (Cholesky-correlated GBM, no external deps)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, versioned, in-memory)
        │
        ▼
   SSE stream (/api/stream/prices) → frontend EventSource
```

**Strengths, confirmed on read:**
- `PriceUpdate` (`models.py`) is a proper immutable value object (`frozen=True, slots=True`); `change`/`change_percent`/`direction` are derived properties, so they can't drift from `price`/`previous_price`.
- `PriceCache` (`cache.py`) is a small, correct, single-responsibility class. The version counter for SSE change-detection (`stream.py:75-83`) is a nice touch — avoids re-serializing unchanged data every 500ms tick when the underlying source (e.g. Massive at a 15s poll) hasn't produced anything new.
- The GBM math in `simulator.py` is textbook-correct: `S(t+dt) = S(t) * exp((mu - sigma²/2)dt + sigma*sqrt(dt)*Z)`, which is numerically stable and structurally guarantees positive prices (the property `test_prices_are_positive` checks over 10,000 steps).
- Cholesky-correlated shocks across a sector correlation matrix (tech 0.6, finance 0.5, cross/TSLA 0.3) is a genuinely nice realism touch for a course capstone, and it's unit tested (`test_pairwise_correlation_*`).
- Both data sources seed the cache synchronously in `start()`/`add_ticker()` before any background loop runs, so there's no blank-price window on startup — this matters for a good first-load experience.
- Both background loops (`_run_loop` in `simulator.py`, `_poll_loop`/`_poll_once` in `massive_client.py`) wrap their per-tick work in `try/except Exception`, so one bad tick (a parsing error, a transient 429) can't kill the feed. `stop()` is cancel-then-await-then-swallow-`CancelledError` in both, and is idempotent.
- `factory.py` is trivial and correct: `MASSIVE_API_KEY` set + non-empty (after `.strip()`) → Massive, else simulator. Whitespace-only keys correctly fall through to the simulator (`test_creates_simulator_when_api_key_whitespace`).
- The design doc (`planning/MARKET_DATA_DESIGN.md` §16) already self-identifies the biggest structural gap (see §3.1 below) rather than hiding it — worth acknowledging, since it means whoever builds the watchlist API next isn't discovering this cold.

---

## 3. Findings

### 3.1 `get_price_history` / sparkline pre-population is not implemented (Severity: High — blocks a specified feature)

`planning/PLAN.md` §6 specifies the market data interface must expose:

```python
def get_price_history(self, ticker: str, n: int = 60) -> list[PriceUpdate]: ...
```

backed by "a rolling deque of the last 60 updates per ticker," used by `GET /api/watchlist` (PLAN.md §8) to pre-populate sparklines on page load without an empty-chart flash.

**The shipped `PriceCache` only stores the single latest `PriceUpdate` per ticker** (`cache.py:19`, `self._prices: dict[str, PriceUpdate]`) — there is no history deque, and neither `MarketDataSource` nor either concrete implementation exposes anything resembling `get_price_history`. This isn't a bug in the code that exists — it's a scope gap, and it's already flagged honestly in `planning/MARKET_DATA_DESIGN.md` §16 ("Known Gap vs. PLAN.md"). It's called out here again because it directly blocks a concrete, specified endpoint contract (`GET /api/watchlist`) that the next agent to touch this code will need, and because `MARKET_DATA_SUMMARY.md` (which is what CLAUDE.md tells agents to read first) says "Status: Complete, tested, reviewed, all issues resolved" without mentioning it — someone reading only the summary would not know this gap exists.

**Recommendation:** before or during the watchlist API build-out, add a bounded `collections.deque(maxlen=60)` per ticker to `PriceCache` alongside the existing latest-value map, plus a `get_history(ticker, n=60) -> list[PriceUpdate]` reader, and wire it through both `SimulatorDataSource` and `MassiveDataSource` (trivial — both already call `cache.update()` on every tick/poll, so the deque appends for free). This does touch the public `PriceCache` surface and needs its own tests, so it's sized as a small follow-up task rather than something to silently bolt on.

### 3.2 Ticker case normalization is inconsistent between the two `MarketDataSource` implementations (Severity: Medium)

`MassiveDataSource.add_ticker`/`remove_ticker` (`massive_client.py:66-76`) normalize with `.upper().strip()` before touching internal state:

```python
async def add_ticker(self, ticker: str) -> None:
    ticker = ticker.upper().strip()
    if ticker not in self._tickers:
        self._tickers.append(ticker)
```

`SimulatorDataSource.add_ticker`/`remove_ticker` (`simulator.py:242-255`) and `GBMSimulator.add_ticker`/`remove_ticker` (`simulator.py:120-134`) do **not** normalize — they use whatever string is passed straight into `self._prices[ticker]`. `PriceCache.update()` (`cache.py:23`) likewise does no normalization.

Concretely: `await source.add_ticker("aapl")` against `SimulatorDataSource` creates a cache entry keyed `"aapl"`, distinct from the `"AAPL"` seeded at startup — two independent price series for what a user would consider the same ticker. The same call against `MassiveDataSource` correctly normalizes to `"AAPL"`. Since `create_market_data_source()` (`factory.py`) picks the implementation transparently based on an env var, the same watchlist-API code would behave differently in dev (simulator) vs. prod (Massive) depending on whether callers already normalize tickers upstream.

**Recommendation:** normalize once, in one place — either have the future watchlist API route normalize (`ticker.upper().strip()`) before calling `source.add_ticker()`/`remove_ticker()` at all (simplest, keeps `MarketDataSource` implementations dumb), or push normalization down into `SimulatorDataSource`/`GBMSimulator` to match `MassiveDataSource`'s existing behavior. Either works; just pick one and don't rely on callers being careful.

### 3.3 No dedicated test for `stream.py` (Severity: Medium)

Covered in §1. `stream.py` owns the actual wire contract the frontend depends on (SSE event framing, the version-diff skip-unchanged logic, the `retry:` directive, disconnect handling), and it's the one module with zero test coverage. A single `httpx.AsyncClient`-based integration test (connect, assert first event is `retry: 1000\n\n`, push a cache update, assert the next event's JSON matches `PriceCache.get_all()`) would cover the highest-risk-of-regression code in the package.

### 3.4 `PriceCache.version` read outside the lock (Severity: Low)

```python
@property
def version(self) -> int:
    return self._version
```

(`cache.py:64-67`) reads `_version` without acquiring `self._lock`, unlike every other method on the class. On CPython with the GIL this is safe (single `int` read is atomic), but it's inconsistent with the rest of the class's discipline, and would become a real (if minor) race under a no-GIL Python build. Low priority given the project's stated scale (single user, ≤ dozens of tickers), but cheap to fix for consistency — wrap it in `with self._lock:` like the others.

### 3.5 `PriceCache.update()` treats a `timestamp=0.0` as "not provided" (Severity: Trivial)

```python
ts = timestamp or time.time()
```

(`cache.py:30`) — if a caller ever explicitly passes `timestamp=0.0` (Unix epoch), the `or` falls through to `time.time()` instead of honoring the explicit zero, because `0.0` is falsy. Not reachable today (Massive timestamps are real epoch-ms values divided down, never legitimately 0), but `timestamp: float | None = None` combined with `if timestamp is not None else time.time()` would be the more correct idiom and costs nothing.

### 3.6 Module-level `router` in `stream.py` (Severity: Low, latent test footgun)

`stream.py:17` creates `router = APIRouter(...)` at module scope, and `create_stream_router()` registers a route on that shared router via closure every time it's called. In production this factory runs once at app startup, so it's harmless today. But it means calling `create_stream_router()` twice (e.g., from two independent tests, or a future hot-reload path) double-registers the `/prices` route on the same module-level router object. If §3.3 is addressed and a test calls this factory more than once across the suite, this will bite. Cheap fix: build a fresh `APIRouter()` inside the factory function instead of module scope.

### 3.7 `README.md` documents an invalid `uv` invocation (Severity: Trivial, docs only)

`backend/README.md:25` and `:48` say `uv sync --dev`. `uv`'s actual flag for pulling in `pyproject.toml`'s `[project.optional-dependencies].dev` group is `uv sync --extra dev` (as correctly documented in `backend/CLAUDE.md:6` and used throughout this review). `uv sync --dev` is not a real `uv` flag — a developer following only `README.md` would hit an error on the very first setup step. Fix the two occurrences to match `CLAUDE.md`.

### 3.8 Short sleep intervals in async lifecycle tests (Severity: Informational)

`test_simulator_source.py` uses update intervals as low as `0.01`s with sleeps of `0.05`s and asserts things like `cache.version > initial_version + 2` (`test_custom_update_interval`, line 123). The margin (expects >2, budget allows ~5) is generous enough to likely be robust, but tests asserting a minimum number of async loop iterations within a fixed wall-clock window are inherently sensitive to CI machine load. Not a correctness bug, just a note that if this test ever flakes intermittently in CI, this is why — not a logic regression.

---

## 4. What's Solid — No Action Needed

- `models.py`, `cache.py`, `factory.py`, `interface.py` are all small, correct, and well-tested (100% coverage per the archived run, and the logic reads as complete on inspection — no missing branches).
- GBM/Cholesky math in `simulator.py` is correct and appropriately tested, including the numerically-important positivity guarantee.
- Exception handling in both background loops is defensive in the right way (catch, log, keep running) — appropriate for a long-lived background service.
- `pyproject.toml` build config, the previously-lazy `massive` import, `_generate_events`'s return-type annotation, `GBMSimulator.get_tickers()`, and the unused-import lint warnings from the prior review all appear fixed in the current code — no regressions found there.

---

## 5. Verdict

The market data backend is well-structured and the core logic (models, cache, simulator, factory) is solid and appropriately tested. Nothing found here blocks continued development, with one exception:

**Should address before/during the watchlist API build-out:**
1. §3.1 — Implement `get_price_history` / rolling deque, since `GET /api/watchlist` (already specified in `PLAN.md`) depends on it for sparkline pre-population.
2. §3.2 — Decide on and enforce one ticker-normalization convention across both data sources before the watchlist API starts calling `add_ticker`/`remove_ticker` with user-supplied strings.

**Should fix, lower urgency:**
3. §3.3 — Add at least one SSE integration test for `stream.py`.
4. §3.7 — Fix the `uv sync --dev` → `uv sync --extra dev` typo in `README.md`.

**Nice to have:**
5. §3.4, §3.5, §3.6 — minor consistency/robustness cleanups, no observed impact at current scale.

**Process note:** this review could not execute the test suite in this session due to sandboxing (see §0). Please run `cd backend && uv run --extra dev pytest -v --cov=app` locally (or grant this workflow broader Bash permissions) to convert the "should pass on inspection" conclusions above into verified ones before relying on them.
