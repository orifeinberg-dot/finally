"""Tests for PriceCache."""

from app.market.cache import PriceCache


class TestPriceCache:
    """Unit tests for the PriceCache."""

    def test_update_and_get(self):
        """Test updating and getting a price."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_first_update_is_flat(self):
        """Test that the first update has flat direction."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        """Test price update with upward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        """Test price update with downward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_remove(self):
        """Test removing a ticker from cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_remove_nonexistent(self):
        """Test removing a ticker that doesn't exist."""
        cache = PriceCache()
        cache.remove("AAPL")  # Should not raise

    def test_get_all(self):
        """Test getting all prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_version_increments(self):
        """Test that version counter increments."""
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_get_price_convenience(self):
        """Test the convenience get_price method."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None

    def test_len(self):
        """Test __len__ method."""
        cache = PriceCache()
        assert len(cache) == 0
        cache.update("AAPL", 190.00)
        assert len(cache) == 1
        cache.update("GOOGL", 175.00)
        assert len(cache) == 2

    def test_contains(self):
        """Test __contains__ method."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert "AAPL" in cache
        assert "GOOGL" not in cache

    def test_custom_timestamp(self):
        """Test updating with a custom timestamp."""
        cache = PriceCache()
        custom_ts = 1234567890.0
        update = cache.update("AAPL", 190.50, timestamp=custom_ts)
        assert update.timestamp == custom_ts

    def test_price_rounding(self):
        """Test that prices are rounded to 2 decimal places."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.12345)
        assert update.price == 190.12

    def test_update_normalizes_ticker(self):
        """Test that update() normalizes ticker case and whitespace."""
        cache = PriceCache()
        update = cache.update("  aapl  ", 190.50)
        assert update.ticker == "AAPL"
        assert cache.get("AAPL") == update

    def test_get_normalizes_ticker(self):
        """Test that get() normalizes ticker case and whitespace."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get(" aapl ") is not None
        assert cache.get("aapl") == cache.get("AAPL")

    def test_get_price_normalizes_ticker(self):
        """Test that get_price() normalizes ticker case and whitespace."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("aapl") == 190.50

    def test_remove_normalizes_ticker(self):
        """Test that remove() normalizes ticker case and whitespace."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove(" aapl ")
        assert cache.get("AAPL") is None

    def test_contains_normalizes_ticker(self):
        """Test that __contains__ normalizes ticker case and whitespace."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert "aapl" in cache
        assert " AAPL " in cache

    def test_mixed_case_updates_hit_same_entry(self):
        """Test that updates with different casing accumulate on one entry."""
        cache = PriceCache()
        cache.update("aapl", 190.00)
        cache.update("AAPL", 191.00)
        cache.update(" Aapl ", 192.00)
        assert len(cache) == 1
        assert cache.get_price("AAPL") == 192.00

    def test_get_history_returns_updates(self):
        """Test that get_history() returns recorded updates, oldest first."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        cache.update("AAPL", 192.00)
        history = cache.get_history("AAPL")
        assert [u.price for u in history] == [190.00, 191.00, 192.00]

    def test_get_history_unknown_ticker(self):
        """Test that get_history() returns an empty list for an unknown ticker."""
        cache = PriceCache()
        assert cache.get_history("NOPE") == []

    def test_get_history_normalizes_ticker(self):
        """Test that get_history() normalizes ticker case and whitespace."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert len(cache.get_history(" aapl ")) == 1

    def test_get_history_capped_at_60(self):
        """Test that history is capped at 60 updates per ticker."""
        cache = PriceCache()
        for i in range(100):
            cache.update("AAPL", 100.0 + i)
        history = cache.get_history("AAPL", n=100)
        assert len(history) == 60
        # Oldest entries should have been evicted; the most recent 60 remain.
        assert [u.price for u in history] == [100.0 + i for i in range(40, 100)]

    def test_get_history_with_n(self):
        """Test get_history(..., n=...) limits the number of returned updates."""
        cache = PriceCache()
        for i in range(10):
            cache.update("AAPL", 100.0 + i)
        history = cache.get_history("AAPL", n=3)
        assert [u.price for u in history] == [107.0, 108.0, 109.0]

    def test_get_history_independent_per_ticker(self):
        """Test that history is tracked independently per ticker."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("GOOGL", 176.00)
        assert len(cache.get_history("AAPL")) == 1
        assert len(cache.get_history("GOOGL")) == 2

    def test_remove_clears_history(self):
        """Test that remove() clears the ticker's history too."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        cache.remove("AAPL")
        assert cache.get_history("AAPL") == []
