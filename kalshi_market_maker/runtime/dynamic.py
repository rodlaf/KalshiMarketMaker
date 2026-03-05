import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple
import threading

import requests

from ..factories import create_api
from ..logging_utils import build_logger
from ..selection.scoring import safe_float, select_top_markets
from .cleanup import stop_worker_then_cancel
from .workers import run_market_worker


def run_dynamic_strategy(dynamic_config: Dict):
    logger = build_logger("DynamicSelector", dynamic_config.get("log_level", "INFO"))

    selector_cfg = dynamic_config.get("market_selector", {})
    refresh_seconds = safe_float(selector_cfg.get("refresh_seconds", 20), 20.0)
    series_ticker = selector_cfg.get("series_ticker")
    mve_filter = selector_cfg.get("mve_filter", "exclude")
    page_limit = int(selector_cfg.get("page_limit", 250))
    max_pages = int(selector_cfg.get("max_pages", 5))
    max_markets = int(selector_cfg.get("max_markets", 1250))

    selector_api = create_api(dynamic_config.get("api", {}), logger, market_ticker="DYNAMIC")
    active_workers: Dict[str, Tuple[threading.Event, object]] = {}
    max_workers = int(selector_cfg.get("top_n", 8)) + 1
    last_selected_tickers: List[str] = []
    shared_risk_state = {"active_markets": 1}
    selector_backoff_seconds = 5.0
    max_selector_backoff_seconds = 120.0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            while True:
                markets: List[Dict] = []
                selected_tickers = last_selected_tickers

                try:
                    markets = selector_api.list_all_open_markets(
                        series_ticker=series_ticker,
                        mve_filter=mve_filter,
                        page_limit=page_limit,
                        max_pages=max_pages,
                        max_markets=max_markets,
                    )
                    ranked = select_top_markets(markets, selector_cfg)
                    selected_tickers = [ticker for ticker, _, _, _ in ranked]
                    last_selected_tickers = selected_tickers
                    selector_backoff_seconds = 5.0
                except requests.exceptions.HTTPError as http_error:
                    status_code = http_error.response.status_code if http_error.response is not None else None
                    if status_code == 429:
                        logger.warning(
                            f"Selector rate-limited (429). Reusing previous selection and backing off for "
                            f"{selector_backoff_seconds:.1f}s"
                        )
                        time.sleep(selector_backoff_seconds)
                        selector_backoff_seconds = min(
                            selector_backoff_seconds * 2,
                            max_selector_backoff_seconds,
                        )
                    else:
                        logger.error(f"Selector HTTP error ({status_code}): {http_error}")
                        time.sleep(selector_backoff_seconds)
                except requests.exceptions.RequestException as request_exception:
                    logger.error(f"Selector request error: {request_exception}")
                    time.sleep(selector_backoff_seconds)

                selected_set = set(selected_tickers)
                shared_risk_state["active_markets"] = max(1, len(selected_tickers))
                logger.info(f"Selector found {len(markets)} open markets; selected: {selected_tickers}")

                for ticker in list(active_workers.keys()):
                    stop_event, future = active_workers[ticker]
                    if ticker not in selected_set:
                        logger.warning(f"Draining deselected ticker {ticker}: stop worker then cancel resting orders")
                        is_clean = stop_worker_then_cancel(
                            ticker,
                            stop_event,
                            future,
                            dynamic_config,
                            logger,
                        )
                        if is_clean:
                            del active_workers[ticker]
                        else:
                            logger.error(
                                f"Could not fully clean up {ticker}; keeping worker state for next retry cycle"
                            )

                for ticker in selected_tickers:
                    if ticker not in active_workers:
                        logger.info(f"Starting worker for selected ticker {ticker}")
                        stop_event = threading.Event()
                        future = executor.submit(
                            run_market_worker,
                            ticker,
                            dynamic_config,
                            stop_event,
                            shared_risk_state,
                        )
                        active_workers[ticker] = (stop_event, future)

                time.sleep(refresh_seconds)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down dynamic strategy")
        finally:
            for ticker in list(active_workers.keys()):
                stop_event, future = active_workers[ticker]
                logger.warning(f"Final shutdown cleanup for {ticker}")
                stop_worker_then_cancel(ticker, stop_event, future, dynamic_config, logger)
            selector_api.logout()
