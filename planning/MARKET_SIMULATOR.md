# Market Simulator Design

Approach and code structure for simulating realistic stock prices when `MASSIVE_API_KEY` is not set — the default mode for FinAlly. Documents `backend/app/market/simulator.py` and `seed_prices.py` as actually implemented.

## Overview

The simulator uses **Geometric Brownian Motion (GBM)**, the standard continuous-time model underlying Black-Scholes: prices evolve as a random walk with drift, can't go negative, and produce the lognormal-ish distribution of returns real markets exhibit. Ticks run every ~500ms, producing a stream of price changes that feels alive rather than a static number.

## GBM Math

At each time step:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift (expected return), e.g. `0.05` for 5%/year
- `sigma` — annualized volatility, e.g. `0.20` for 20%/year
- `dt` — time step as a fraction of a trading year
- `Z` — a (correlated) standard normal draw

For 500ms ticks against a 252-day, 6.5-hour trading year:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8
```

This tiny `dt` is what keeps per-tick moves sub-cent and realistic — plug in a `dt` of 1 (i.e. one full year per tick) and the `exp()` would blow the price up or down by an absurd multiple on the first tick. The multiplicative `exp()` form also guarantees `S(t+dt) > 0` always, no matter how bad the random draw — a stock price can approach zero but never cross it.

## Correlated Moves via Cholesky Decomposition

Real stocks don't move independently — tech names tend to move together on macro news, etc. Given a target correlation matrix `C`, computing `L = cholesky(C)` and applying it to independent standard normals `Z_independent` produces normals with exactly that correlation structure:

```
Z_correlated = L @ Z_independent
```

Correlation structure (`seed_prices.py`):

```python
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
INTRA_TECH_CORR = 0.6      # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR = 0.3     # cross-sector / unknown tickers
TSLA_CORR = 0.3            # TSLA is a loner even though it's grouped with tech
```

Pairwise correlation lookup (`GBMSimulator._pairwise_correlation`):

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR                       # checked before the tech-set check
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

TSLA is a member of the `"tech"` set (for display/grouping purposes elsewhere) but is special-cased *first* here so it never gets the 0.6 intra-tech correlation — it's meant to behave more independently, matching its real-world volatility profile.

The correlation matrix is rebuilt (`_rebuild_cholesky`) whenever a ticker is added or removed — O(n²), but n stays under ~50 tickers so this is cheap. With 0 or 1 tickers there's nothing to correlate, so `_cholesky` is `None` and `step()` just uses independent normals.

## Random Shock Events

Every tick, every ticker has a small independent chance of a sudden 2-5% move — pure visual drama, not part of the GBM model itself:

```python
if random.random() < event_probability:   # default 0.001 (0.1%)
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

At 2 ticks/sec, 0.1% per ticker per tick works out to roughly one event every ~500 seconds per ticker — with a 10-ticker watchlist, expect something newsworthy on the dashboard roughly every ~50 seconds.

## Seed Prices & Per-Ticker Parameters

`seed_prices.py` holds pure data, no logic:

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

A ticker added dynamically via the watchlist (not in `SEED_PRICES`) starts at a random price in `[50, 300]` and uses `DEFAULT_PARAMS` for its GBM parameters. This means every ticker the simulator has ever seen — seeded or added later — is treated identically once it's in `_prices`/`_params`; there's no special-casing beyond the initial lookup.

## Implementation

```python
# app/market/simulator.py
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT, event_probability: float = 0.001):
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance every tracked ticker by one tick. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:      # rebuilds Cholesky
    def remove_ticker(self, ticker: str) -> None:   # rebuilds Cholesky
    def get_price(self, ticker: str) -> float | None: ...
    def get_tickers(self) -> list[str]: ...
```

`step()` is intentionally allocation-light in the hot loop: one `np.random.standard_normal(n)` call and one matmul per tick, not per-ticker RNG calls, since this runs twice a second for the lifetime of the process.

## SimulatorDataSource — wiring into the `MarketDataSource` interface

`GBMSimulator` itself knows nothing about asyncio, caches, or the rest of the app — it's pure math over a `dict[str, float]`. `SimulatorDataSource` (in the same file) wraps it to satisfy the `MarketDataSource` ABC (see `MARKET_INTERFACE.md`): an `asyncio` task calls `sim.step()` every `update_interval` seconds (default 0.5s) and writes each result into the shared `PriceCache`. This separation is what makes `GBMSimulator` trivially unit-testable (deterministic given a seeded RNG, no async, no I/O) while the `SimulatorDataSource` wrapper is what the rest of the app actually depends on.

## File Structure

```
backend/app/market/
  simulator.py      # GBMSimulator (pure math) + SimulatorDataSource (async wrapper)
  seed_prices.py     # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
```

`seed_prices.py` is data-only by design — tuning volatility/drift/correlations for a ticker never requires touching simulation logic in `simulator.py`.

## Behavior Notes

- Prices never go negative — GBM's multiplicative `exp()` step is always strictly positive.
- The tiny `dt` produces sub-cent moves per tick that compound into realistic hour/day-scale ranges; `sigma=0.50` (TSLA) produces roughly the right intraday range over a simulated trading day.
- Correlation matrices from real-valued correlation coefficients in `[-1, 1]` with 1s on the diagonal are positive semi-definite by construction here (fixed small set of coefficients: 0.6 / 0.5 / 0.3), so `np.linalg.cholesky` never fails in practice — but this is a property of the specific coefficients chosen, not a general guarantee for arbitrary correlation matrices.
- Adding a ticker mid-session rebuilds the whole Cholesky decomposition (`O(n^2)`); fine for tens of tickers, would need a smarter incremental update if the watchlist ever grew to hundreds.
- Both `SEED_PRICES` and `TICKER_PARAMS` are point-in-time realistic values as of project creation — they drift from real market prices over time since nothing refreshes them, which is fine for a simulator (only the seed/starting point, not accuracy, matters).
