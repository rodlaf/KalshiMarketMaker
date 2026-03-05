import math
import time
from typing import Dict, List, Tuple, Optional

from .interfaces import AbstractTradingAPI


class AvellanedaMarketMaker:
    def __init__(
        self,
        logger,
        api: AbstractTradingAPI,
        gamma: float,
        k: float,
        sigma: float,
        T: float,
        max_position: int,
        order_expiration: int,
        min_spread: float = 0.01,
        position_limit_buffer: float = 0.1,
        inventory_skew_factor: float = 0.01,
        trade_side: str = "yes",
        max_global_contracts: Optional[int] = None,
        max_contracts_per_market: Optional[int] = None,
        reserve_contracts_buffer: int = 0,
        shared_risk_state: Optional[Dict] = None,
    ):
        self.api = api
        self.logger = logger
        self.base_gamma = gamma
        self.k = k
        self.sigma = sigma
        self.T = T
        self.max_position = max_position
        self.order_expiration = order_expiration
        self.min_spread = min_spread
        self.position_limit_buffer = position_limit_buffer
        self.inventory_skew_factor = inventory_skew_factor
        self.trade_side = trade_side
        self.max_global_contracts = max_global_contracts
        self.max_contracts_per_market = max_contracts_per_market
        self.reserve_contracts_buffer = max(0, int(reserve_contracts_buffer))
        self.shared_risk_state = shared_risk_state or {"active_markets": 1}

    def run(self, dt: float, stop_event=None):
        start_time = time.time()
        while time.time() - start_time < self.T:
            if stop_event is not None and stop_event.is_set():
                self.logger.info("Stop signal received, shutting down market maker loop")
                break

            current_time = time.time() - start_time
            mid_prices = self.api.get_price()
            mid_price = mid_prices[self.trade_side]
            inventory = self.api.get_position()

            reservation_price = self.calculate_reservation_price(mid_price, inventory, current_time)
            bid_price, ask_price = self.calculate_asymmetric_quotes(mid_price, inventory, current_time)
            current_orders = self.api.get_orders()
            buy_size, sell_size = self.calculate_order_sizes(inventory, current_orders)

            self.logger.info(
                f"t={current_time:.2f}s mid={mid_price:.4f} inventory={inventory} "
                f"reservation={reservation_price:.4f} bid={bid_price:.4f} ask={ask_price:.4f}"
            )

            self.manage_orders(bid_price, ask_price, buy_size, sell_size, current_orders)
            time.sleep(dt)

        self.logger.info("Avellaneda market maker finished running")

    def calculate_asymmetric_quotes(self, mid_price: float, inventory: int, elapsed_time: float) -> Tuple[float, float]:
        reservation_price = self.calculate_reservation_price(mid_price, inventory, elapsed_time)
        base_spread = self.calculate_optimal_spread(elapsed_time, inventory)

        effective_max_position = self.get_effective_max_position()
        position_ratio = inventory / effective_max_position
        spread_adjustment = base_spread * abs(position_ratio) * 3

        if inventory > 0:
            bid_spread = base_spread / 2 + spread_adjustment
            ask_spread = max(base_spread / 2 - spread_adjustment, self.min_spread / 2)
        else:
            bid_spread = max(base_spread / 2 - spread_adjustment, self.min_spread / 2)
            ask_spread = base_spread / 2 + spread_adjustment

        bid_price = max(0.01, min(mid_price, reservation_price - bid_spread))
        ask_price = min(0.99, max(mid_price, reservation_price + ask_spread))

        return bid_price, ask_price

    def calculate_reservation_price(self, mid_price: float, inventory: int, elapsed_time: float) -> float:
        dynamic_gamma = self.calculate_dynamic_gamma(inventory)
        inventory_skew = -inventory * self.inventory_skew_factor * mid_price
        return mid_price + inventory_skew - inventory * dynamic_gamma * (self.sigma ** 2) * (1 - elapsed_time / self.T)

    def calculate_optimal_spread(self, elapsed_time: float, inventory: int) -> float:
        dynamic_gamma = self.calculate_dynamic_gamma(inventory)
        time_remaining = max(0.0, 1 - elapsed_time / self.T)
        base_spread = (
            dynamic_gamma * (self.sigma ** 2) * time_remaining
            + (2 / dynamic_gamma) * math.log(1 + (dynamic_gamma / self.k))
        )
        effective_max_position = self.get_effective_max_position()
        position_ratio = min(1.0, abs(inventory) / effective_max_position)
        spread_multiplier = 1 + 2.0 * position_ratio
        return max(base_spread * spread_multiplier, self.min_spread)

    def calculate_dynamic_gamma(self, inventory: int) -> float:
        effective_max_position = self.get_effective_max_position()
        position_ratio = abs(inventory) / effective_max_position
        return self.base_gamma * (1 + (position_ratio**2) * 4)

    def get_effective_max_position(self) -> int:
        if self.max_contracts_per_market is not None:
            configured_market_cap = max(1, int(self.max_contracts_per_market))
        else:
            configured_market_cap = max(1, int(self.max_position))

        if self.max_global_contracts is None:
            return configured_market_cap

        active_markets = max(1, int(self.shared_risk_state.get("active_markets", 1)))
        global_budget = max(1, int(self.max_global_contracts) - self.reserve_contracts_buffer)
        equal_weight_cap = max(1, global_budget // active_markets)
        return max(1, min(configured_market_cap, equal_weight_cap))

    def get_global_remaining_capacity(self) -> int:
        if self.max_global_contracts is None:
            return 10**9

        try:
            positions = self.api.list_all_positions()
            total_abs_position = sum(abs(int(float(position.get("position", 0)))) for position in positions)
            remaining = int(self.max_global_contracts) - self.reserve_contracts_buffer - total_abs_position
            return max(0, remaining)
        except Exception as global_exception:
            self.logger.error(f"Global risk snapshot failed, blocking new risk: {global_exception}")
            return 0

    def extract_pending_exposure(self, current_orders: List[Dict]) -> Tuple[int, int]:
        pending_buy = 0
        pending_sell = 0

        for order in current_orders:
            if order.get("side") != self.trade_side:
                continue
            remaining = int(float(order.get("remaining_count", 0)))
            if order.get("action") == "buy":
                pending_buy += remaining
            elif order.get("action") == "sell":
                pending_sell += remaining

        return pending_buy, pending_sell

    def calculate_order_sizes(self, inventory: int, current_orders: List[Dict]) -> Tuple[int, int]:
        effective_max_position = self.get_effective_max_position()
        pending_buy, pending_sell = self.extract_pending_exposure(current_orders)
        effective_inventory = inventory + pending_buy - pending_sell

        local_remaining_capacity = max(0, effective_max_position - abs(effective_inventory))
        global_remaining_capacity = self.get_global_remaining_capacity()

        base_size = max(1, int(effective_max_position * self.position_limit_buffer))
        accumulation_size = min(base_size, local_remaining_capacity, global_remaining_capacity)
        if global_remaining_capacity <= 0:
            accumulation_size = 0

        reduction_size = max(1, min(effective_max_position, max(base_size, abs(effective_inventory))))

        if effective_inventory > 0:
            buy_size = accumulation_size
            sell_size = reduction_size
        elif effective_inventory < 0:
            buy_size = reduction_size
            sell_size = accumulation_size
        else:
            buy_size = accumulation_size
            sell_size = accumulation_size

        return buy_size, sell_size

    def manage_orders(
        self,
        bid_price: float,
        ask_price: float,
        buy_size: int,
        sell_size: int,
        current_orders: Optional[List[Dict]] = None,
    ):
        if current_orders is None:
            current_orders = self.api.get_orders()

        buy_orders: List[Dict] = []
        sell_orders: List[Dict] = []

        for order in current_orders:
            if order["side"] == self.trade_side:
                if order["action"] == "buy":
                    buy_orders.append(order)
                elif order["action"] == "sell":
                    sell_orders.append(order)

        self.handle_order_side("buy", buy_orders, bid_price, buy_size)
        self.handle_order_side("sell", sell_orders, ask_price, sell_size)

    def handle_order_side(self, action: str, orders: List[Dict], desired_price: float, desired_size: int):
        keep_order = None

        for order in orders:
            current_price = (
                float(order["yes_price"]) / 100
                if self.trade_side == "yes"
                else float(order["no_price"]) / 100
            )
            if (
                keep_order is None
                and abs(current_price - desired_price) < 0.01
                and order["remaining_count"] == desired_size
            ):
                keep_order = order
            else:
                self.api.cancel_order(order["order_id"])

        if desired_size <= 0:
            return

        current_price = self.api.get_price()[self.trade_side]
        should_place = (action == "buy" and desired_price < current_price) or (
            action == "sell" and desired_price > current_price
        )

        if keep_order is None and should_place:
            self.api.place_order(
                action,
                self.trade_side,
                desired_price,
                desired_size,
                int(time.time()) + self.order_expiration,
            )
