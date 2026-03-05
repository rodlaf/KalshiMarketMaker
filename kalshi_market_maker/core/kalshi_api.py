import base64
import logging
import random
import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .interfaces import AbstractTradingAPI


class KalshiTradingAPI(AbstractTradingAPI):
    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        market_ticker: str,
        base_url: str,
        logger: logging.Logger,
    ):
        if not api_key_id:
            raise ValueError("KALSHI_API_KEY_ID environment variable is required")
        if not private_key_path:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH environment variable is required")

        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.market_ticker = market_ticker
        self.logger = logger
        self.base_url = base_url.rstrip("/")
        self.private_key = self.load_private_key()
        self.logger.info("Kalshi API client initialized")

    def load_private_key(self):
        with open(self.private_key_path, "rb") as private_key_file:
            return serialization.load_pem_private_key(private_key_file.read(), password=None)

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
        self,
        method: str,
        path: str,
        params: Dict = None,
        data: Dict = None,
        max_retries: int = 5,
    ):
        url = f"{self.base_url}{path}"
        parsed_path = urlparse(url).path
        retryable_codes = {429, 500, 502, 503, 504}

        for attempt in range(max_retries + 1):
            headers = self.get_headers(method, parsed_path)
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=data,
                    timeout=15,
                )

                if response.status_code in retryable_codes and attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay_seconds = float(retry_after)
                        except ValueError:
                            delay_seconds = 0.0
                    else:
                        delay_seconds = 0.0

                    backoff = max(delay_seconds, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                    self.logger.warning(
                        f"Retryable response {response.status_code} for {method} {path}; retrying in {backoff:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as request_exception:
                if attempt < max_retries:
                    backoff = 0.5 * (2**attempt) + random.uniform(0, 0.25)
                    self.logger.warning(
                        f"Request exception for {method} {path}: {request_exception}. Retrying in {backoff:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(backoff)
                    continue

                self.logger.error(f"Request failed: {request_exception}")
                if hasattr(request_exception, "response") and request_exception.response is not None:
                    self.logger.error(f"Response content: {request_exception.response.text}")
                raise

    def get_position(self) -> int:
        path = "/portfolio/positions"
        params = {"ticker": self.market_ticker, "settlement_status": "unsettled"}
        response = self.make_request("GET", path, params=params)
        positions = response.get("market_positions", [])

        total_position = 0
        for position in positions:
            if position["ticker"] == self.market_ticker:
                total_position += position["position"]

        return total_position

    def get_price(self) -> Dict[str, float]:
        path = f"/markets/{self.market_ticker}"
        data = self.make_request("GET", path)

        yes_bid = float(data["market"]["yes_bid"]) / 100
        yes_ask = float(data["market"]["yes_ask"]) / 100
        no_bid = float(data["market"]["no_bid"]) / 100
        no_ask = float(data["market"]["no_ask"]) / 100

        yes_mid_price = round((yes_bid + yes_ask) / 2, 2)
        no_mid_price = round((no_bid + no_ask) / 2, 2)

        return {"yes": yes_mid_price, "no": no_mid_price}

    def place_order(self, action: str, side: str, price: float, quantity: int, expiration_ts: int = None) -> str:
        return self.place_order_for_ticker(
            ticker=self.market_ticker,
            action=action,
            side=side,
            price=price,
            quantity=quantity,
            expiration_ts=expiration_ts,
        )

    def place_order_for_ticker(
        self,
        ticker: str,
        action: str,
        side: str,
        price: float,
        quantity: int,
        expiration_ts: int = None,
    ) -> str:
        path = "/portfolio/orders"
        data = {
            "ticker": ticker,
            "action": action.lower(),
            "type": "limit",
            "side": side,
            "count": quantity,
            "client_order_id": str(uuid.uuid4()),
        }

        price_to_send = int(price * 100)
        if side == "yes":
            data["yes_price"] = price_to_send
        else:
            data["no_price"] = price_to_send

        if expiration_ts is not None:
            data["expiration_ts"] = expiration_ts

        response = self.make_request("POST", path, data=data)
        return str(response["order"]["order_id"])

    def get_market(self, ticker: str) -> Dict:
        path = f"/markets/{ticker}"
        return self.make_request("GET", path)

    def list_all_positions(
        self,
        page_limit: int = 200,
        max_pages: int = 20,
        count_filter: str = "position",
    ) -> List[Dict]:
        positions: List[Dict] = []
        cursor = None
        pages = 0

        safe_page_limit = max(1, min(1000, page_limit))
        safe_max_pages = max(1, max_pages)

        while True:
            path = "/portfolio/positions"
            params = {"limit": safe_page_limit, "count_filter": count_filter}
            if cursor:
                params["cursor"] = cursor

            response = self.make_request("GET", path, params=params)
            batch = response.get("market_positions", [])
            positions.extend(batch)

            pages += 1
            cursor = response.get("cursor")

            if not cursor or pages >= safe_max_pages:
                break

        return positions

    def cancel_order(self, order_id: int) -> bool:
        path = f"/portfolio/orders/{order_id}"
        response = self.make_request("DELETE", path)
        return response["reduced_by"] > 0

    def get_orders(self, ticker: Optional[str] = None, status: str = "resting") -> List[Dict]:
        path = "/portfolio/orders"
        effective_ticker = self.market_ticker if ticker is None else ticker
        params = {"status": status}
        if effective_ticker:
            params["ticker"] = effective_ticker

        response = self.make_request("GET", path, params=params)
        return response.get("orders", [])

    def list_all_resting_orders(
        self,
        ticker: Optional[str] = None,
        page_limit: int = 200,
        max_pages: int = 20,
    ) -> List[Dict]:
        orders: List[Dict] = []
        cursor = None
        pages = 0

        safe_page_limit = max(1, min(1000, page_limit))
        safe_max_pages = max(1, max_pages)

        while True:
            path = "/portfolio/orders"
            params = {"status": "resting", "limit": safe_page_limit}
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor

            response = self.make_request("GET", path, params=params)
            batch = response.get("orders", [])
            orders.extend(batch)

            pages += 1
            cursor = response.get("cursor")

            if not cursor or pages >= safe_max_pages:
                break

        return orders

    def list_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        cursor: str = None,
        series_ticker: str = None,
        mve_filter: str = "exclude",
    ) -> Dict:
        path = "/markets"
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if mve_filter:
            params["mve_filter"] = mve_filter
        return self.make_request("GET", path, params=params)

    def list_all_open_markets(
        self,
        series_ticker: str = None,
        mve_filter: str = "exclude",
        page_limit: int = 250,
        max_pages: int = 5,
        max_markets: int = 1250,
    ) -> List[Dict]:
        markets: List[Dict] = []
        cursor = None
        pages = 0

        safe_page_limit = max(1, min(1000, page_limit))
        safe_max_pages = max(1, max_pages)
        safe_max_markets = max(1, max_markets)

        while True:
            response = self.list_markets(
                status="open",
                limit=safe_page_limit,
                cursor=cursor,
                series_ticker=series_ticker,
                mve_filter=mve_filter,
            )
            batch = response.get("markets", [])
            markets.extend(batch)
            pages += 1
            cursor = response.get("cursor")

            if len(markets) >= safe_max_markets or pages >= safe_max_pages or not cursor:
                break

        return markets[:safe_max_markets]
