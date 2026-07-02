"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from .models import PriceUpdate

DEFAULT_HISTORY_SIZE = 60


class PriceCache:
    """Thread-safe in-memory cache of the latest price and rolling history per ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.

    Tickers are normalized with `.upper().strip()` on every read/write so
    callers don't need to worry about case or whitespace inconsistencies.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[PriceUpdate]] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    @staticmethod
    def _normalize(ticker: str) -> str:
        return ticker.upper().strip()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
        ticker = self._normalize(ticker)
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            history = self._history.setdefault(ticker, deque(maxlen=DEFAULT_HISTORY_SIZE))
            history.append(update)
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        ticker = self._normalize(ticker)
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_history(self, ticker: str, n: int = DEFAULT_HISTORY_SIZE) -> list[PriceUpdate]:
        """Get up to the last `n` price updates for a ticker, oldest first.

        Returns an empty list if the ticker is unknown.
        """
        ticker = self._normalize(ticker)
        with self._lock:
            history = self._history.get(ticker)
            if not history:
                return []
            return list(history)[-n:]

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        ticker = self._normalize(ticker)
        with self._lock:
            self._prices.pop(ticker, None)
            self._history.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        ticker = self._normalize(ticker)
        with self._lock:
            return ticker in self._prices
