import abc
import time
import base64
from typing import Dict, List, Tuple
import requests
import logging
import uuid
import math
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

class AbstractTradingAPI(abc.ABC):
    @abc.abstractmethod
    def get_price(self) -> float:
        pass

    @abc.abstractmethod
    def place_order(self, action: str, side: str, price: float, quantity: int, expiration_ts: int = None) -> str:
        pass

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass

    @abc.abstractmethod
    def get_position(self) -> int:
        pass

    @abc.abstractmethod
    def get_orders(self) -> List[Dict]:
        pass

class KalshiTradingAPI(AbstractTradingAPI):
    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        market_ticker: str,
        base_url: str,
        logger: logging.Logger,
    ):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.market_ticker = market_ticker
        self.logger = logger
        self.base_url = base_url.rstrip("/")
        self.private_key = self.load_private_key()
        self.logger.info("Kalshi API client initialized with API key auth")

    def load_private_key(self):
        with open(self.private_key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def logout(self):
        return None

    def _create_signature(self, timestamp: str, method: str, path: str) -> str:
        sign_path = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def get_headers(self, method: str, path: str):
        timestamp = str(int(time.time() * 1000))
        signature = self._create_signature(timestamp, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def make_request(
        self, method: str, path: str, params: Dict = None, data: Dict = None
    ):
        url = f"{self.base_url}{path}"
        parsed_path = urlparse(url).path
        headers = self.get_headers(method, parsed_path)

        try:
            response = requests.request(
                method, url, headers=headers, params=params, json=data
            )
            self.logger.debug(f"Request URL: {response.url}")
            self.logger.debug(f"Request headers: {response.request.headers}")
            self.logger.debug(f"Request params: {params}")
            self.logger.debug(f"Request data: {data}")
            self.logger.debug(f"Response status code: {response.status_code}")
            self.logger.debug(f"Response content: {response.text}")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {e}")
            if hasattr(e, "response") and e.response is not None:
                self.logger.error(f"Response content: {e.response.text}")
            raise

    def get_position(self) -> int:
        self.logger.info("Retrieving position...")
        path = "/portfolio/positions"
        params = {"ticker": self.market_ticker, "settlement_status": "unsettled"}
        response = self.make_request("GET", path, params=params)
        positions = response.get("market_positions", [])

        total_position = 0
        for position in positions:
            if position["ticker"] == self.market_ticker:
                total_position += position["position"]

        self.logger.info(f"Current position: {total_position}")
        return total_position

    def get_price(self) -> Dict[str, float]:
        self.logger.info("Retrieving market data...")
        path = f"/markets/{self.market_ticker}"
        data = self.make_request("GET", path)

        yes_bid = float(data["market"]["yes_bid"]) / 100
        yes_ask = float(data["market"]["yes_ask"]) / 100
        no_bid = float(data["market"]["no_bid"]) / 100
        no_ask = float(data["market"]["no_ask"]) / 100
        
        yes_mid_price = round((yes_bid + yes_ask) / 2, 2)
        no_mid_price = round((no_bid + no_ask) / 2, 2)

        self.logger.info(f"Current yes mid-market price: ${yes_mid_price:.2f}")
        self.logger.info(f"Current no mid-market price: ${no_mid_price:.2f}")
        return {"yes": yes_mid_price, "no": no_mid_price}

    def place_order(self, action: str, side: str, price: float, quantity: int, expiration_ts: int = None) -> str:
        self.logger.info(f"Placing {action} order for {side} side at price ${price:.2f} with quantity {quantity}...")
        path = "/portfolio/orders"
        data = {
            "ticker": self.market_ticker,
            "action": action.lower(),  # 'buy' or 'sell'
            "type": "limit",
            "side": side,  # 'yes' or 'no'
            "count": quantity,
            "client_order_id": str(uuid.uuid4()),
        }
        price_to_send = int(price * 100) # Convert dollars to cents

        if side == "yes":
            data["yes_price"] = price_to_send
        else:
            data["no_price"] = price_to_send

        if expiration_ts is not None:
            data["expiration_ts"] = expiration_ts

        try:
            response = self.make_request("POST", path, data=data)
            order_id = response["order"]["order_id"]
            self.logger.info(f"Placed {action} order for {side} side at price ${price:.2f} with quantity {quantity}, order ID: {order_id}")
            return str(order_id)
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to place order: {e}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response content: {e.response.text}")
                self.logger.error(f"Request data: {data}")
            raise

    def cancel_order(self, order_id: int) -> bool:
        self.logger.info(f"Canceling order with ID {order_id}...")
        path = f"/portfolio/orders/{order_id}"
        response = self.make_request("DELETE", path)
        success = response["reduced_by"] > 0
        self.logger.info(f"Canceled order with ID {order_id}, success: {success}")
        return success

    def get_orders(self) -> List[Dict]:
        self.logger.info("Retrieving orders...")
        path = "/portfolio/orders"
        params = {"ticker": self.market_ticker, "status": "resting"}
        response = self.make_request("GET", path, params=params)
        orders = response.get("orders", [])
        self.logger.info(f"Retrieved {len(orders)} orders")
        return orders

class AvellanedaMarketMaker:
    def __init__(
        self,
        logger: logging.Logger,
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
        trade_side: str = "yes"
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

    def run(self, dt: float):
        start_time = time.time()
        while time.time() - start_time < self.T:
            current_time = time.time() - start_time
            self.logger.info(f"Running Avellaneda market maker at {current_time:.2f}")

            mid_prices = self.api.get_price()
            mid_price = mid_prices[self.trade_side]
            inventory = self.api.get_position()
            self.logger.info(f"Current mid price for {self.trade_side}: {mid_price:.4f}, Inventory: {inventory}")

            reservation_price = self.calculate_reservation_price(mid_price, inventory, current_time)
            bid_price, ask_price = self.calculate_asymmetric_quotes(mid_price, inventory, current_time)
            buy_size, sell_size = self.calculate_order_sizes(inventory)

            self.logger.info(f"Reservation price: {reservation_price:.4f}")
            self.logger.info(f"Computed desired bid: {bid_price:.4f}, ask: {ask_price:.4f}")

            self.manage_orders(bid_price, ask_price, buy_size, sell_size)

            time.sleep(dt)

        self.logger.info("Avellaneda market maker finished running")

    def calculate_asymmetric_quotes(self, mid_price: float, inventory: int, t: float) -> Tuple[float, float]:
        reservation_price = self.calculate_reservation_price(mid_price, inventory, t)
        base_spread = self.calculate_optimal_spread(t, inventory)
        
        position_ratio = inventory / self.max_position
        spread_adjustment = base_spread * abs(position_ratio) * 3
        
        if inventory > 0:
            bid_spread = base_spread / 2 + spread_adjustment
            ask_spread = max(base_spread / 2 - spread_adjustment, self.min_spread / 2)
        else:
            bid_spread = max(base_spread / 2 - spread_adjustment, self.min_spread / 2)
            ask_spread = base_spread / 2 + spread_adjustment
        
        bid_price = max(0, min(mid_price, reservation_price - bid_spread))
        ask_price = min(1, max(mid_price, reservation_price + ask_spread))
        
        return bid_price, ask_price

    def calculate_reservation_price(self, mid_price: float, inventory: int, t: float) -> float:
        dynamic_gamma = self.calculate_dynamic_gamma(inventory)
        inventory_skew = inventory * self.inventory_skew_factor * mid_price
        return mid_price + inventory_skew - inventory * dynamic_gamma * (self.sigma**2) * (1 - t/self.T)

    def calculate_optimal_spread(self, t: float, inventory: int) -> float:
        dynamic_gamma = self.calculate_dynamic_gamma(inventory)
        base_spread = (dynamic_gamma * (self.sigma**2) * (1 - t/self.T) + 
                       (2 / dynamic_gamma) * math.log(1 + (dynamic_gamma / self.k)))
        position_ratio = abs(inventory) / self.max_position
        spread_adjustment = 1 - (position_ratio ** 2)
        return max(base_spread * spread_adjustment * 0.01, self.min_spread)

    def calculate_dynamic_gamma(self, inventory: int) -> float:
        position_ratio = inventory / self.max_position
        return self.base_gamma * math.exp(-abs(position_ratio))

    def calculate_order_sizes(self, inventory: int) -> Tuple[int, int]:
        remaining_capacity = self.max_position - abs(inventory)
        buffer_size = int(self.max_position * self.position_limit_buffer)
        
        if inventory > 0:
            buy_size = max(1, min(buffer_size, remaining_capacity))
            sell_size = max(1, self.max_position)
        else:
            buy_size = max(1, self.max_position)
            sell_size = max(1, min(buffer_size, remaining_capacity))
        
        return buy_size, sell_size

    def manage_orders(self, bid_price: float, ask_price: float, buy_size: int, sell_size: int):
        current_orders = self.api.get_orders()
        self.logger.info(f"Retrieved {len(current_orders)} total orders")

        buy_orders = []
        sell_orders = []

        for order in current_orders:
            if order['side'] == self.trade_side:
                if order['action'] == 'buy':
                    buy_orders.append(order)
                elif order['action'] == 'sell':
                    sell_orders.append(order)

        self.logger.info(f"Current buy orders: {len(buy_orders)}")
        self.logger.info(f"Current sell orders: {len(sell_orders)}")

        # Handle buy orders
        self.handle_order_side('buy', buy_orders, bid_price, buy_size)

        # Handle sell orders
        self.handle_order_side('sell', sell_orders, ask_price, sell_size)

    def handle_order_side(self, action: str, orders: List[Dict], desired_price: float, desired_size: int):
        keep_order = None
        for order in orders:
            current_price = float(order['yes_price']) / 100 if self.trade_side == 'yes' else float(order['no_price']) / 100
            if keep_order is None and abs(current_price - desired_price) < 0.01 and order['remaining_count'] == desired_size:
                keep_order = order
                self.logger.info(f"Keeping existing {action} order. ID: {order['order_id']}, Price: {current_price:.4f}")
            else:
                self.logger.info(f"Cancelling extraneous {action} order. ID: {order['order_id']}, Price: {current_price:.4f}")
                self.api.cancel_order(order['order_id'])

        current_price = self.api.get_price()[self.trade_side]
        if keep_order is None:
            if (action == 'buy' and desired_price < current_price) or (action == 'sell' and desired_price > current_price):
                try:
                    order_id = self.api.place_order(action, self.trade_side, desired_price, desired_size, int(time.time()) + self.order_expiration)
                    self.logger.info(f"Placed new {action} order. ID: {order_id}, Price: {desired_price:.4f}, Size: {desired_size}")
                except Exception as e:
                    self.logger.error(f"Failed to place {action} order: {str(e)}")
            else:
                self.logger.info(f"Skipped placing {action} order. Desired price {desired_price:.4f} does not improve on current price {current_price:.4f}")
