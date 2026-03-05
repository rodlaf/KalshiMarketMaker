from typing import Dict, List, Tuple


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_spread_cents(market: Dict) -> float:
    yes_bid = safe_float(market.get("yes_bid"), -1)
    yes_ask = safe_float(market.get("yes_ask"), -1)
    if yes_bid < 0 or yes_ask < 0:
        return -1
    return yes_ask - yes_bid


def is_supported_binary_market(market: Dict) -> bool:
    market_type = str(market.get("market_type", "binary")).lower()
    ticker = str(market.get("ticker", ""))

    # Reject non-binary markets (scalar, etc.)
    if market_type != "binary":
        return False

    # Reject MVE (multivariate event) markets.
    # The API's mve_filter=exclude SHOULD filter these, but KXMVE* markets
    # have been observed slipping through. Multiple layers of defense:
    #
    # 1. Hard ticker-prefix gate: all MVE market tickers start with "KXMVE"
    if ticker.upper().startswith("KXMVE"):
        return False

    # 2. API response fields: mve_collection_ticker / mve_selected_legs
    #    (x-omitempty in the spec means absent when empty, present when set)
    if market.get("mve_collection_ticker"):
        return False
    mve_legs = market.get("mve_selected_legs")
    if mve_legs is not None and len(mve_legs) > 0:
        return False

    # 3. MVE combos use strike_type="functional" — reject those too
    strike_type = str(market.get("strike_type", "")).lower()
    if strike_type == "functional":
        return False

    return True


def select_top_markets(markets: List[Dict], selector_cfg: Dict) -> List[Tuple[str, float, float, float]]:
    min_volume_24h = safe_float(selector_cfg.get("min_volume_24h", 100))
    min_spread_cents = safe_float(selector_cfg.get("min_spread_cents", 1))
    top_n = int(selector_cfg.get("top_n", 8))
    volume_weight = safe_float(selector_cfg.get("volume_weight", 0.5))
    spread_weight = safe_float(selector_cfg.get("spread_weight", 0.5))

    candidates = []
    for market in markets:
        if not is_supported_binary_market(market):
            continue

        ticker = market.get("ticker")
        if not ticker:
            continue

        volume_24h = safe_float(market.get("volume_24h", market.get("volume", 0)))
        spread_cents = compute_spread_cents(market)

        if volume_24h < min_volume_24h or spread_cents < min_spread_cents:
            continue

        candidates.append(
            {
                "ticker": ticker,
                "volume_24h": volume_24h,
                "spread_cents": spread_cents,
            }
        )

    if not candidates:
        return []

    volumes = [market["volume_24h"] for market in candidates]
    spreads = [market["spread_cents"] for market in candidates]
    min_volume, max_volume = min(volumes), max(volumes)
    min_spread, max_spread = min(spreads), max(spreads)

    def normalize(value: float, low: float, high: float) -> float:
        if high == low:
            return 1.0
        return (value - low) / (high - low)

    ranked = []
    for market in candidates:
        volume_norm = normalize(market["volume_24h"], min_volume, max_volume)
        spread_norm = 1.0 - normalize(market["spread_cents"], min_spread, max_spread)
        score = volume_weight * volume_norm + spread_weight * spread_norm
        ranked.append((market["ticker"], score, market["volume_24h"], market["spread_cents"]))

    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked[:top_n]
