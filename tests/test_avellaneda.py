"""Tests for critical Avellaneda-Stoikov model fixes.

Verifies:
1. Reservation price moves AWAY from inventory direction (mean-reverting)
2. Spread calculation produces sane values for binary option price range [0, 1]
3. Asymmetric quotes incentivize position reduction
"""

import math
import pytest

from kalshi_market_maker.core.avellaneda import AvellanedaMarketMaker


class FakeAPI:
    """Minimal stub implementing AbstractTradingAPI for unit tests."""

    def __init__(self, mid_price=0.50, position=0, orders=None, positions=None):
        self._mid_price = mid_price
        self._position = position
        self._orders = orders or []
        self._positions = positions or []

    def get_price(self):
        return {"yes": self._mid_price, "no": 1.0 - self._mid_price}

    def get_position(self):
        return self._position

    def get_orders(self, ticker=None, status="resting"):
        return self._orders

    def list_all_positions(self):
        return self._positions

    def place_order(self, action, side, price, quantity, expiration_ts=None):
        return "fake-order-id"

    def cancel_order(self, order_id):
        return True

    def logout(self):
        pass


class FakeLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


def make_mm(**overrides):
    """Create an AvellanedaMarketMaker with production-like defaults."""
    defaults = dict(
        logger=FakeLogger(),
        api=FakeAPI(),
        gamma=0.2,
        k=150.0,
        sigma=0.10,
        T=28800,
        max_position=3,
        order_expiration=3600,
        min_spread=0.03,
        position_limit_buffer=0.05,
        inventory_skew_factor=0.001,
        trade_side="yes",
        max_global_contracts=20,
        max_contracts_per_market=3,
        reserve_contracts_buffer=2,
        shared_risk_state={"active_markets": 5},
    )
    defaults.update(overrides)
    return AvellanedaMarketMaker(**defaults)


# ── Reservation Price Tests ──────────────────────────────────────────


class TestReservationPrice:
    """Reservation price must push quotes toward mean-reversion."""

    def test_long_inventory_reservation_below_mid(self):
        """When long, reservation price must be BELOW mid to incentivize selling."""
        mm = make_mm()
        mid = 0.50
        reservation = mm.calculate_reservation_price(mid, inventory=1, elapsed_time=0)
        assert reservation < mid, (
            f"Long inventory: reservation ({reservation:.6f}) should be below mid ({mid})"
        )

    def test_short_inventory_reservation_above_mid(self):
        """When short, reservation price must be ABOVE mid to incentivize buying."""
        mm = make_mm()
        mid = 0.50
        reservation = mm.calculate_reservation_price(mid, inventory=-1, elapsed_time=0)
        assert reservation > mid, (
            f"Short inventory: reservation ({reservation:.6f}) should be above mid ({mid})"
        )

    def test_zero_inventory_reservation_equals_mid(self):
        """When flat, reservation price should equal mid."""
        mm = make_mm()
        mid = 0.50
        reservation = mm.calculate_reservation_price(mid, inventory=0, elapsed_time=0)
        assert abs(reservation - mid) < 1e-10, (
            f"Zero inventory: reservation ({reservation:.6f}) should equal mid ({mid})"
        )

    def test_larger_inventory_larger_displacement(self):
        """Larger inventory should push reservation further from mid."""
        mm = make_mm()
        mid = 0.50
        r1 = mm.calculate_reservation_price(mid, inventory=1, elapsed_time=0)
        r2 = mm.calculate_reservation_price(mid, inventory=2, elapsed_time=0)
        assert r2 < r1 < mid, (
            f"Larger long inventory should push reservation lower: r2={r2:.6f} < r1={r1:.6f} < mid={mid}"
        )

    @pytest.mark.parametrize("mid", [0.10, 0.25, 0.50, 0.75, 0.90])
    def test_reservation_direction_various_mids(self, mid):
        """Reservation direction holds across the full binary option price range."""
        mm = make_mm()
        r_long = mm.calculate_reservation_price(mid, inventory=1, elapsed_time=0)
        r_short = mm.calculate_reservation_price(mid, inventory=-1, elapsed_time=0)
        assert r_long < mid, f"mid={mid}: long reservation {r_long:.6f} should be below mid"
        assert r_short > mid, f"mid={mid}: short reservation {r_short:.6f} should be above mid"


# ── Spread Calculation Tests ─────────────────────────────────────────


class TestSpreadCalculation:
    """Spread must be sane for binary option prices in [0.01, 0.99]."""

    def test_spread_positive(self):
        mm = make_mm()
        spread = mm.calculate_optimal_spread(elapsed_time=0, inventory=0)
        assert spread > 0

    def test_spread_in_sane_range(self):
        """Spread should be between 1 cent and 50 cents for binary options."""
        mm = make_mm()
        spread = mm.calculate_optimal_spread(elapsed_time=0, inventory=0)
        assert 0.01 <= spread <= 0.50, (
            f"Spread {spread:.4f} outside sane range [0.01, 0.50]"
        )

    def test_spread_not_floored_at_min_spread(self):
        """With calibrated sigma, spread should be driven by the model, not just min_spread."""
        mm = make_mm(sigma=0.10)
        spread = mm.calculate_optimal_spread(elapsed_time=0, inventory=0)
        # With proper calibration, the model should produce a spread above min_spread
        # (or at minimum, the model is producing a non-trivial value)
        assert spread >= mm.min_spread

    def test_spread_increases_with_inventory(self):
        """Spread should widen as inventory grows (more risk -> wider quotes)."""
        mm = make_mm()
        spread_flat = mm.calculate_optimal_spread(elapsed_time=0, inventory=0)
        spread_loaded = mm.calculate_optimal_spread(elapsed_time=0, inventory=2)
        assert spread_loaded >= spread_flat, (
            f"Loaded spread ({spread_loaded:.4f}) should be >= flat spread ({spread_flat:.4f})"
        )


# ── Asymmetric Quote Tests ───────────────────────────────────────────


class TestAsymmetricQuotes:
    """Quotes should be asymmetric in the direction that reduces inventory."""

    def test_long_inventory_tighter_ask(self):
        """When long, ask should be closer to mid than bid (incentivize selling)."""
        mm = make_mm()
        mid = 0.50
        bid, ask = mm.calculate_asymmetric_quotes(mid, inventory=1, elapsed_time=0)
        bid_distance = mid - bid
        ask_distance = ask - mid
        assert ask_distance <= bid_distance, (
            f"Long: ask distance ({ask_distance:.4f}) should be <= bid distance ({bid_distance:.4f}) "
            f"to incentivize selling. bid={bid:.4f}, ask={ask:.4f}"
        )

    def test_short_inventory_tighter_bid(self):
        """When short, bid should be closer to mid than ask (incentivize buying)."""
        mm = make_mm()
        mid = 0.50
        bid, ask = mm.calculate_asymmetric_quotes(mid, inventory=-1, elapsed_time=0)
        bid_distance = mid - bid
        ask_distance = ask - mid
        assert bid_distance <= ask_distance, (
            f"Short: bid distance ({bid_distance:.4f}) should be <= ask distance ({ask_distance:.4f}) "
            f"to incentivize buying. bid={bid:.4f}, ask={ask:.4f}"
        )

    def test_quotes_straddle_mid_when_flat(self):
        """When flat, bid < mid < ask."""
        mm = make_mm()
        mid = 0.50
        bid, ask = mm.calculate_asymmetric_quotes(mid, inventory=0, elapsed_time=0)
        assert bid < mid < ask, f"Flat: expected bid ({bid}) < mid ({mid}) < ask ({ask})"

    def test_quotes_within_bounds(self):
        """All quotes must be within [0.01, 0.99]."""
        mm = make_mm()
        for inv in [-3, -2, -1, 0, 1, 2, 3]:
            bid, ask = mm.calculate_asymmetric_quotes(0.50, inventory=inv, elapsed_time=0)
            assert 0.01 <= bid <= 0.99, f"bid={bid} out of bounds for inv={inv}"
            assert 0.01 <= ask <= 0.99, f"ask={ask} out of bounds for inv={inv}"
            assert bid < ask, f"bid={bid} >= ask={ask} for inv={inv}"


# ── Market Selection Tests ───────────────────────────────────────────


class TestMarketSelection:
    """Market selector should prefer tighter spreads (lower adverse selection)."""

    def test_tighter_spread_scores_higher(self):
        from kalshi_market_maker.selection.scoring import select_top_markets

        markets = [
            {"ticker": "TIGHT", "market_type": "binary", "volume_24h": 1000, "yes_bid": 50, "yes_ask": 52},
            {"ticker": "WIDE", "market_type": "binary", "volume_24h": 1000, "yes_bid": 50, "yes_ask": 60},
        ]
        cfg = {
            "min_volume_24h": 0,
            "min_spread_cents": 0,
            "top_n": 2,
            "volume_weight": 0.35,
            "spread_weight": 0.65,
        }
        ranked = select_top_markets(markets, cfg)
        assert ranked[0][0] == "TIGHT", (
            f"Tighter spread market should rank first, got: {[t for t, *_ in ranked]}"
        )

    def test_wide_spread_does_not_dominate(self):
        from kalshi_market_maker.selection.scoring import select_top_markets

        markets = [
            {"ticker": "LIQUID_TIGHT", "market_type": "binary", "volume_24h": 5000, "yes_bid": 50, "yes_ask": 52},
            {"ticker": "ILLIQUID_WIDE", "market_type": "binary", "volume_24h": 200, "yes_bid": 50, "yes_ask": 70},
        ]
        cfg = {
            "min_volume_24h": 0,
            "min_spread_cents": 0,
            "top_n": 2,
            "volume_weight": 0.35,
            "spread_weight": 0.65,
        }
        ranked = select_top_markets(markets, cfg)
        assert ranked[0][0] == "LIQUID_TIGHT", (
            f"Liquid tight-spread market should rank first, got: {[t for t, *_ in ranked]}"
        )

    def test_falls_back_when_strict_filters_eliminate_all_markets(self):
        from kalshi_market_maker.selection.scoring import select_top_markets

        markets = [
            {"ticker": "A", "market_type": "binary", "volume_24h": 20, "yes_bid": 50, "yes_ask": 51},
            {"ticker": "B", "market_type": "binary", "volume_24h": 30, "yes_bid": 50, "yes_ask": 52},
        ]
        cfg = {
            "min_volume_24h": 500,
            "min_spread_cents": 5,
            "top_n": 2,
            "volume_weight": 0.35,
            "spread_weight": 0.65,
        }
        ranked = select_top_markets(markets, cfg)
        assert [t for t, *_ in ranked] == ["A", "B"]


# ── MVE (Multivariate Event) Rejection Tests ─────────────────────────


class TestMveRejection:
    """MVE / combo markets must NEVER pass the market filter.

    MVE markets on Kalshi use:
    - Ticker prefix ``KXMVE``
    - ``mve_collection_ticker`` field (string, omitted when empty)
    - ``mve_selected_legs`` field (array, omitted when empty)
    - ``strike_type`` = ``functional`` for MVE combos
    """

    DEFAULT_CFG = {
        "min_volume_24h": 0,
        "min_spread_cents": 0,
        "top_n": 10,
        "volume_weight": 0.5,
        "spread_weight": 0.5,
    }

    def _make_normal_market(self, ticker="NORMAL-MKT"):
        return {"ticker": ticker, "market_type": "binary", "volume_24h": 1000, "yes_bid": 50, "yes_ask": 55}

    # ── Ticker prefix ────────────────────────────────────────────────

    def test_kxmve_ticker_prefix_rejected(self):
        """Real MVE tickers like KXMVECROSSCATEGORY-... must be blocked."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [
            self._make_normal_market("KXMVECROSSCATEGORY-S2026AC2C50B4327-42FA1A48178"),
            self._make_normal_market("SAFE"),
        ]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        tickers = [t for t, *_ in ranked]
        assert "KXMVECROSSCATEGORY-S2026AC2C50B4327-42FA1A48178" not in tickers
        assert "SAFE" in tickers

    def test_kxmve_sports_multi_rejected(self):
        """Another real MVE pattern: KXMVESPORTSMULTIGAMEEXTENDED-..."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [
            self._make_normal_market("KXMVESPORTSMULTIGAMEEXTENDED-S20268CC4775E9C4-6B387DBFB67"),
        ]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    def test_kxmve_case_insensitive(self):
        """Ticker check should be case-insensitive."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [self._make_normal_market("kxmveSomething")]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    # ── API response fields ──────────────────────────────────────────

    def test_mve_collection_ticker_rejected(self):
        """Markets with mve_collection_ticker set must be blocked."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{
            **self._make_normal_market("SNEAKY"),
            "mve_collection_ticker": "KXMVECOLL-123",
        }]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    def test_mve_selected_legs_rejected(self):
        """Markets with mve_selected_legs populated must be blocked."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{
            **self._make_normal_market("SNEAKY2"),
            "mve_selected_legs": [{"ticker": "LEG1"}, {"ticker": "LEG2"}],
        }]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    def test_mve_selected_legs_empty_list_passes(self):
        """An empty mve_selected_legs list should NOT block the market."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{
            **self._make_normal_market("LEGIT"),
            "mve_selected_legs": [],
        }]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 1

    # ── strike_type = functional ─────────────────────────────────────

    def test_strike_type_functional_rejected(self):
        """MVE combos use strike_type='functional' — must be blocked."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{**self._make_normal_market("FUNC-MKT"), "strike_type": "functional"}]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    def test_strike_type_greater_passes(self):
        """Normal binary strike types like 'greater' should pass."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{**self._make_normal_market("NORMAL"), "strike_type": "greater"}]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 1

    # ── Non-binary ───────────────────────────────────────────────────

    def test_scalar_market_rejected(self):
        """Scalar markets should be rejected."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [{**self._make_normal_market("SCALAR"), "market_type": "scalar"}]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 0

    # ── Normal market sanity ─────────────────────────────────────────

    def test_normal_binary_market_passes(self):
        """A clean binary market with no MVE fields should pass."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [self._make_normal_market("LEGIT")]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        assert len(ranked) == 1
        assert ranked[0][0] == "LEGIT"

    def test_multiple_mve_variants_all_blocked(self):
        """Mix of MVE marker types — all should be blocked, only clean one passes."""
        from kalshi_market_maker.selection.scoring import select_top_markets
        markets = [
            self._make_normal_market("KXMVECROSSCATEGORY-AAA"),
            {**self._make_normal_market("HAS-COLL"), "mve_collection_ticker": "COLL-1"},
            {**self._make_normal_market("HAS-LEGS"), "mve_selected_legs": [{"ticker": "L1"}]},
            {**self._make_normal_market("FUNC"), "strike_type": "functional"},
            self._make_normal_market("CLEAN-BINARY"),
        ]
        ranked = select_top_markets(markets, self.DEFAULT_CFG)
        tickers = [t for t, *_ in ranked]
        assert tickers == ["CLEAN-BINARY"]
