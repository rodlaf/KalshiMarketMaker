import argparse
import curses
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv

from ..factories import create_api
from ..logging_utils import build_logger


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_money(value, assume_cents: bool = True) -> float:
    if value is None:
        return 0.0

    if isinstance(value, int):
        return value / 100.0 if assume_cents else float(value)

    if isinstance(value, float):
        return value

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return 0.0
        if "." in cleaned:
            return safe_float(cleaned)
        parsed_int = safe_int(cleaned)
        return parsed_int / 100.0 if assume_cents else float(parsed_int)

    parsed = safe_float(value)
    return parsed / 100.0 if assume_cents else parsed


def money_from_fields(payload: Dict, dollars_key: str, raw_key: str) -> float:
    dollars_value = payload.get(dollars_key)
    if dollars_value is not None:
        return normalize_money(dollars_value, assume_cents=False)

    raw_value = payload.get(raw_key)
    return normalize_money(raw_value, assume_cents=True)


def draw_line(stdscr, row: int, text: str, width: int, attr=0):
    if row < 0:
        return
    clipped = text[: max(0, width - 1)]
    stdscr.addstr(row, 0, clipped.ljust(max(0, width - 1)), attr)


def summarize_positions(positions: List[Dict]) -> Dict[str, float]:
    non_zero = [p for p in positions if safe_int(p.get("position", 0)) != 0]
    total_abs_contracts = sum(abs(safe_int(p.get("position", 0))) for p in non_zero)
    net_contracts = sum(safe_int(p.get("position", 0)) for p in non_zero)
    realized_pnl = sum(money_from_fields(p, "realized_pnl_dollars", "realized_pnl") for p in non_zero)
    market_exposure = sum(money_from_fields(p, "market_exposure_dollars", "market_exposure") for p in non_zero)

    return {
        "non_zero_markets": len(non_zero),
        "total_abs_contracts": total_abs_contracts,
        "net_contracts": net_contracts,
        "realized_pnl": realized_pnl,
        "market_exposure": market_exposure,
    }


def collect_snapshot(api, logger, fetch_balance: bool, balance_supported: bool):
    positions = api.list_all_positions()
    active_statuses = ["resting", "open"]
    combined_orders: List[Dict] = []
    seen_order_ids = set()

    for status in active_statuses:
        try:
            orders_for_status = api.list_all_orders_by_status(status=status)
        except Exception as orders_exception:
            logger.warning(f"Order fetch failed for status={status}: {orders_exception}")
            continue

        for order in orders_for_status:
            order_id = order.get("order_id")
            dedupe_key = str(order_id) if order_id is not None else f"{status}-{len(combined_orders)}"
            if dedupe_key in seen_order_ids:
                continue
            seen_order_ids.add(dedupe_key)
            order_copy = dict(order)
            order_copy["_dashboard_status"] = status
            combined_orders.append(order_copy)

    balance = None
    if fetch_balance and balance_supported:
        try:
            balance = api.make_request("GET", "/portfolio/balance", max_retries=1)
        except Exception as balance_exception:
            logger.warning(f"Balance endpoint unavailable: {balance_exception}")
            balance_supported = False

    return positions, combined_orders, balance, balance_supported


def render_dashboard(stdscr, args, api, logger):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)

    last_positions: List[Dict] = []
    last_orders: List[Dict] = []
    last_balance: Optional[Dict] = None
    last_error: str = ""
    balance_supported = True
    refresh_counter = 0

    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break

        refresh_counter += 1
        fetch_balance_now = refresh_counter % max(1, args.balance_every_n) == 0

        try:
            positions, orders, balance, balance_supported = collect_snapshot(
                api,
                logger,
                fetch_balance=fetch_balance_now,
                balance_supported=balance_supported,
            )
            last_positions = positions
            last_orders = orders
            if balance is not None:
                last_balance = balance
            last_error = ""
        except Exception as dashboard_exception:
            last_error = str(dashboard_exception)

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        header = f"Kalshi Dashboard | refresh={args.refresh_seconds:.1f}s | q=quit"
        draw_line(stdscr, 0, header, width, curses.A_BOLD)
        draw_line(stdscr, 1, f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}", width)

        summary = summarize_positions(last_positions)

        cash_line = "Cash: n/a"
        if last_balance is not None:
            if isinstance(last_balance, dict):
                balance_obj = last_balance.get("balance", last_balance)
                if isinstance(balance_obj, dict):
                    available_cash = money_from_fields(
                        balance_obj,
                        "available_balance_dollars",
                        "available_balance",
                    )
                    cash_line = f"Cash Available: ${available_cash:,.2f}"
                else:
                    cash_line = f"Cash Available: ${normalize_money(balance_obj, assume_cents=True):,.2f}"
            else:
                cash_line = f"Cash Available: ${normalize_money(last_balance, assume_cents=True):,.2f}"

        draw_line(
            stdscr,
            3,
            (
                f"{cash_line} | Realized PnL: ${summary['realized_pnl']:,.2f} | "
                f"Market Exposure(API): ${summary['market_exposure']:,.2f}"
            ),
            width,
        )
        draw_line(
            stdscr,
            4,
            (
                f"Markets with Position: {summary['non_zero_markets']} | "
                f"Abs Contracts: {summary['total_abs_contracts']} | Net Contracts: {summary['net_contracts']}"
            ),
            width,
        )
        status_counts = {}
        for order in last_orders:
            status_value = order.get("_dashboard_status", order.get("status", "unknown"))
            status_counts[status_value] = status_counts.get(status_value, 0) + 1
        status_summary = ", ".join(f"{key}:{value}" for key, value in sorted(status_counts.items()))
        draw_line(stdscr, 5, f"Active Orders: {len(last_orders)} ({status_summary})", width)

        if last_error:
            draw_line(stdscr, 6, f"Last Error: {last_error}", width)

        positions_header_row = 8
        draw_line(stdscr, positions_header_row, "Positions (ticker | pos | exposure$ | realized_pnl$)", width, curses.A_UNDERLINE)

        sorted_positions = sorted(
            [p for p in last_positions if safe_int(p.get("position", 0)) != 0],
            key=lambda p: abs(safe_int(p.get("position", 0))),
            reverse=True,
        )

        row = positions_header_row + 1
        max_rows_for_positions = max(3, (height - 14) // 2)
        for position in sorted_positions[:max_rows_for_positions]:
            draw_line(
                stdscr,
                row,
                (
                    f"{position.get('ticker', 'UNKNOWN'):<36} | "
                    f"{safe_int(position.get('position', 0)):>6} | "
                    f"{money_from_fields(position, 'market_exposure_dollars', 'market_exposure'):>10.2f} | "
                    f"{money_from_fields(position, 'realized_pnl_dollars', 'realized_pnl'):>10.2f}"
                ),
                width,
            )
            row += 1

        orders_header_row = positions_header_row + 1 + max_rows_for_positions + 1
        draw_line(stdscr, orders_header_row, "Active Orders (status | ticker | id | action | side | remaining)", width, curses.A_UNDERLINE)

        row = orders_header_row + 1
        max_rows_for_orders = max(3, height - row - 1)
        for order in last_orders[:max_rows_for_orders]:
            draw_line(
                stdscr,
                row,
                (
                    f"{str(order.get('_dashboard_status', order.get('status', 'n/a'))):<7} | "
                    f"{order.get('ticker', 'UNKNOWN'):<30} | "
                    f"{str(order.get('order_id', 'n/a')):<12} | "
                    f"{str(order.get('action', 'n/a')):<4} | "
                    f"{str(order.get('side', 'n/a')):<3} | "
                    f"{safe_int(order.get('remaining_count', 0)):>6}"
                ),
                width,
            )
            row += 1

        stdscr.refresh()
        time.sleep(max(0.2, args.refresh_seconds))


def main():
    parser = argparse.ArgumentParser(description="Realtime terminal dashboard for Kalshi account state")
    parser.add_argument("--refresh-seconds", type=float, default=2.0, help="Dashboard refresh interval")
    parser.add_argument(
        "--balance-every-n",
        type=int,
        default=15,
        help="Fetch balance endpoint every N refresh cycles (reduces API load)",
    )
    parser.add_argument("--log-level", type=str, default="WARNING", help="Logger level")
    args = parser.parse_args()

    load_dotenv()
    logger = build_logger("Dashboard", args.log_level)
    api = create_api({}, logger, market_ticker="DYNAMIC")

    try:
        curses.wrapper(lambda stdscr: render_dashboard(stdscr, args, api, logger))
    finally:
        api.logout()


if __name__ == "__main__":
    main()