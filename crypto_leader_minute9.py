"""Single-coin 15-minute Kalshi markets: minute-nine buy, +5c gross exit.

For BTC/ETH/SOL/XRP/HYPE individual UP/DOWN contracts, this strategy buys
whichever side has a quoted ask from 59c through 91c during elapsed minute 9.
No basket mean, leader rank, direction indicator or other coin-selection rule.
Both sides qualifying is ambiguous, so the market is skipped.

Standalone preview mode by default. Live mode requires --live AND an explicit
M9_TRADING_ENABLED=true setting. DO NOT run alongside another bot that adopts
all outside buys as manual inventory: competing take-profits could oversell.
No stop is created. IOC exits require the process to stay running.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_CEILING
from pathlib import Path

import requests
from dotenv import load_dotenv

from kalshi import KalshiClient
from price_pairs import _outcome_cost

DEFAULT_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
MIN_PRICE, MAX_PRICE, PROFIT = D(".59"), D(".91"), D(".05")
TERMINAL = {"executed", "canceled", "cancelled", "expired", "rejected"}


def event(name, **details):
    print(json.dumps({"event": name, "at": datetime.now(timezone.utc).isoformat(),
                      **details}, default=str), flush=True)


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Venue timestamps must include a timezone")
    return parsed.timestamp()


def plan_single_coin(market, now, series):
    """Return one eligible YES/NO ask in [59, 91]c at 9:00–9:59 elapsed."""
    ticker = str(market.get("ticker", ""))
    if not ticker.startswith(series + "-") or market.get("status") not in {"active", "open"}:
        return None
    try:
        close = parse_time(market["close_time"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    # Each 15-minute series settles a fixed 900s after the contract window opens.
    open_at = close - 900
    if not 540 <= now - open_at < 600:
        return None
    candidates = []
    for side in ("YES", "NO"):
        try:
            ask = D(str(market[side.lower() + "_ask_dollars"]))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        if not ask.is_finite() or ask < MIN_PRICE or ask > MAX_PRICE:
            continue
        limit = (ask * 100).to_integral_value(rounding=ROUND_CEILING) / 100
        if limit <= MAX_PRICE:
            candidates.append((side, ask, limit))
    if len(candidates) != 1:
        return None  # Never guess a side if both are eligible.
    side, ask, limit = candidates[0]
    return {"ticker": ticker, "series": series, "side": side, "ask": str(ask),
            "limit": str(limit), "window_start_ms": int(open_at * 1000),
            "window_end_ms": int(close * 1000)}


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            obj = json.loads(self.path.read_text())
            if obj.get("schema") != 1 or not isinstance(obj.get("windows"), dict):
                raise RuntimeError("Invalid minute-9 journal; manual recovery required")
            self.data = obj
        else:
            self.data = {"schema": 1, "windows": {}}
            self.save()

    def save(self):
        # fsync + atomic replacement, including directory, before any write.
        fd, tmp = tempfile.mkstemp(prefix=".minute9-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(self.data, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
            directory = os.open(str(self.path.parent), os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def find_order(client, ticker, intent):
    if intent.get("order_id"):
        order = client.order(intent["order_id"], ticker)
        if order.get("ticker") != ticker:
            raise RuntimeError("Order ticker mismatch")
        return order
    # Lost acknowledgement: reconcile by durable client_order_id. Never repeat POST.
    matching = [row for row in client.all_orders(ticker)
                if row.get("client_order_id") == intent["client_id"]]
    if len(matching) > 1:
        raise RuntimeError("Multiple orders matched one client id")
    if matching:
        intent["order_id"] = matching[0]["order_id"]
        return matching[0]
    return None


def filled_quantity(fills, order_id):
    rows = [f for f in fills if f.get("order_id") == order_id]
    qty = sum((D(str(row.get("count_fp", row.get("count", "0")))) for row in rows), D(0))
    if qty < 0 or qty != qty.to_integral_value():
        raise RuntimeError("Unsupported or invalid fill quantity")
    return qty, rows


def position_for(client, ticker):
    positions = [row for row in client.positions(ticker) if row.get("ticker") == ticker
                 and row.get("subaccount_number", 0) == 0]
    if len(positions) > 1:
        raise RuntimeError("Ambiguous primary-account positions")
    return D(str(positions[0].get("position_fp", "0"))) if positions else D(0)


def reconcile_exit(client, journal, record, now):
    ticker = record["ticker"]
    market = client.market(ticker)
    if parse_time(market["close_time"]) <= now or market.get("status") not in {"active", "open"}:
        return
    buy = find_order(client, ticker, record["buy"])
    if buy is None:
        event("M9_BUY_ACK_UNRESOLVED", ticker=ticker)
        return
    journal.save()
    if buy.get("status") not in TERMINAL:
        return  # IOC must terminate before attempting exits
    all_fills = client.all_fills(ticker)
    bought, buy_rows = filled_quantity(all_fills, record["buy"]["order_id"])
    if bought == 0:
        event("M9_BUY_ZERO_FILL", ticker=ticker)
        return
    if bought > D(record["quantity"]):
        raise RuntimeError("Bought more contracts than authorized")
    sold = D(0)
    for intent in record["sells"]:
        order = find_order(client, ticker, intent)
        if order is None or order.get("status") not in TERMINAL:
            event("M9_SELL_ACK_PENDING", ticker=ticker)
            return  # Never duplicate ambiguous or resting sells
        journal.save()
        count, _ = filled_quantity(all_fills, intent["order_id"])
        if count > D(intent["quantity"]):
            raise RuntimeError("Exit fill exceeds requested quantity")
        # A terminal order can show its status before its fill history.
        expected = order.get("fill_count_fp", order.get("fill_count"))
        if expected is None or D(str(expected)) != count:
            event("M9_SELL_FILL_RECONCILING", ticker=ticker)
            return
        sold += count
    remaining = bought - sold
    if remaining <= 0:
        return
    held = position_for(client, ticker)
    sign = 1 if record["side"] == "YES" else -1
    if held != sign * remaining:  # No netting/over-selling any manual inventory
        event("M9_EXIT_OWNERSHIP_WAIT", ticker=ticker, bought=str(bought),
              sold=str(sold), held=str(held))
        return
    competing = [o for o in client.all_orders(ticker, "resting")
                 if o.get("order_id") not in
                 {item.get("order_id") for item in record["sells"]}]
    if competing:
        event("M9_EXIT_OTHER_ORDER_WAIT", ticker=ticker)
        return
    cost = sum((D(str(f.get("count_fp", f.get("count")))) * _outcome_cost(f, sign)
                for f in buy_rows), D(0)) / bought
    target = (cost + PROFIT).quantize(D(".01"), rounding=ROUND_CEILING)
    if target > D(".99"):
        raise RuntimeError("TP above maximum tradable quote")
    bid = D(str(market[record["side"].lower() + "_bid_dollars"]))
    if bid < target:
        return
    intent = {"client_id": str(uuid.uuid4()), "quantity": str(remaining),
              "target": str(target), "created_at": now}
    record["sells"].append(intent)
    journal.save()  # Intent is durable before any POST
    event("M9_TP_SUBMIT", ticker=ticker, quantity=str(remaining), target=str(target))
    response = client.place_take_profit(ticker, sign * remaining, target,
                                        client_order_id=intent["client_id"])
    row = response.get("order", response)
    if row.get("order_id"):
        intent["order_id"] = row["order_id"]
        journal.save()


def run_pass(client, journal, series_list, quantity, live, now):
    # Check exits first. Never require TradingView or a mean data collector.
    for record in list(journal.data["windows"].values()):
        if record.get("buy"):
            try:
                reconcile_exit(client, journal, record, now)
            except Exception as error:
                event("M9_EXIT_RECONCILIATION_PAUSED", ticker=record["ticker"],
                      error_type=type(error).__name__)
    for series in series_list:
        try:
            markets = client.markets(series_ticker=series, status="open", limit=100)
        except Exception as error:
            event("M9_MARKET_DISCOVERY_PAUSED", series=series, error_type=type(error).__name__)
            continue
        for market in markets:
            plan = plan_single_coin(market, now, series)
            if plan is None:
                continue
            ticker = plan["ticker"]
            if ticker in journal.data["windows"]:
                continue  # One attempt per individual coin market and 15m window.
            if position_for(client, ticker) != 0 or client.all_orders(ticker, "resting"):
                event("M9_ENTRY_BLOCKED_EXISTING_INVENTORY", ticker=ticker)
                continue
            if not live:
                event("M9_DRY_RUN_SIGNAL", **plan, quantity=quantity,
                      target_example=str(D(plan["limit"]) + PROFIT))
                continue
            # Fresh quote, same side, same 15-minute market, and still minute 9.
            market = client.market(ticker)
            current = plan_single_coin(market, time.time(), series)
            if current is None or current["side"] != plan["side"]:
                continue
            intent = {"client_id": str(uuid.uuid4()), "limit": current["limit"],
                      "created_at": time.time()}
            record = {"ticker": ticker, "series": series, "side": current["side"],
                      "quantity": str(quantity), "buy": intent, "sells": []}
            journal.data["windows"][ticker] = record
            journal.save()  # Durably forbid duplicate entry even if ACK is lost.
            event("M9_BUY_SUBMIT", ticker=ticker, side=current["side"],
                  quantity=str(quantity), price=current["limit"])
            response = client.place_entry(ticker, current["side"], quantity, D(current["limit"]),
                parse_time(market["close_time"]), submit_before=(
                    current["window_start_ms"] + 600000) / 1000,
                client_order_id=intent["client_id"], ioc=True)
            row = response.get("order", response)
            if row.get("order_id"):
                intent["order_id"] = row["order_id"]
                journal.save()

def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    if args.live and os.getenv("M9_TRADING_ENABLED", "false").lower() != "true":
        raise RuntimeError("Live mode requires M9_TRADING_ENABLED=true")
    qty = D(os.getenv("M9_QUANTITY", "1"))
    if qty < 1 or qty != qty.to_integral_value() or qty > 20:
        raise ValueError("M9_QUANTITY must be between 1 and 20 whole contracts")
    poll = float(os.getenv("M9_POLL_SECONDS", "2"))
    if not 1 <= poll <= 30:
        raise ValueError("M9_POLL_SECONDS must be between 1 and 30")
    series_list = tuple(s.strip().upper() for s in os.getenv("M9_SERIES", ",".join(DEFAULT_SERIES)).split(",") if s.strip())
    if not series_list or len(set(series_list)) != len(series_list) or any(s not in DEFAULT_SERIES for s in series_list):
        raise ValueError("M9_SERIES must be a unique subset of BTC, ETH, SOL, XRP, HYPE 15m series")
    state = Path(os.getenv("M9_STATE_PATH",
                  "/data/minute9_live.json" if args.live else "/data/minute9_preview.json"))
    if os.getenv("RAILWAY_ENVIRONMENT_ID"):
        mount = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/missing-volume"))
        if not os.path.ismount(mount) or not state.resolve().is_relative_to(mount.resolve()):
            raise RuntimeError("Minute-9 state must reside on the persistent volume")
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix(".lock").open("a") as lock, requests.Session() as session:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        journal = Journal(state)
        client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""),
            os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""),
            timeout=5, role="entry", quote_ttl=0.1, session=session)
        if not client.key_id or client.private_key is None:
            raise RuntimeError("Kalshi API credentials missing")
        event("M9_READY", live=args.live, series=series_list, quantity=str(qty),
              price_range_cents="59-91", take_profit_cents=5, no_stop=True)
        while not stop:
            started = time.monotonic()
            try:
                run_pass(client, journal, series_list, qty, args.live, time.time())
            except Exception as error:
                event("M9_PASS_PAUSED", error_type=type(error).__name__)
            if args.once:
                break
            time.sleep(max(.1, poll - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
