from typing import Dict
import threading

from ..factories import create_api, create_market_maker
from ..logging_utils import build_logger


def run_market_worker(
    ticker: str,
    dynamic_config: Dict,
    stop_event: threading.Event,
    shared_risk_state: Dict | None = None,
):
    logger = build_logger(f"Worker_{ticker}", dynamic_config.get("log_level", "INFO"))

    api = create_api(dynamic_config.get("api", {}), logger, market_ticker=ticker)
    market_maker = create_market_maker(
        dynamic_config.get("market_maker", {}),
        api,
        logger,
        dynamic_config.get("risk", {}),
        shared_risk_state,
    )
    dt = dynamic_config.get("dt", 2.0)

    try:
        logger.info(f"Starting market maker worker for {ticker}")
        market_maker.run(dt, stop_event=stop_event)
    except Exception as worker_exception:
        logger.error(f"Worker failed for {ticker}: {worker_exception}")
    finally:
        api.logout()
