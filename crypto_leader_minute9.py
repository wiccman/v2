"""Crypto Leader: one NO entry on the minute-9 nearest-mean coin, +5c TP.

Standalone strategy, separate from the production manual-exit runner. Disabled by
default. Live operation requires --live and M9_TRADING_ENABLED=true. Exits are
price-protected reduce-only IOCs, NOT resting GTC orders; they require this
process to stay running. No stop-loss is placed.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
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

SERIES = "KXCRYPTOLEAD15M"
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
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


def nearest_mean_coin(sample):
    """Only complete, fresh-standard five-coin readings; skip exact/near ties."""
    if sample.get("missing") != 0 or sample.get("chart_type") != "standard":
        return None
    quality = sample.get("data_quality") or {}
    if quality.get("held_count", 0) or quality.get("prior_window_count", 0):
        return None
    values = sample.get("values") or {}
    try:
        nums = {coin: float(values[coin.lower()]) for coin in COINS}
        mean = float(sample["mean"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(mean) or any(not math.isfinite(v) for v in nums.values()):
        return None
    if abs(sum(nums.values()) / len(COINS) - mean) > 1e-7:
        return None
    ranked = sorted((abs(value - mean), coin) for coin, value in nums.items())
    if abs(ranked[0][0] - ranked[1][0]) <= 1e-9:
        return None
    return ranked[0][1]


def plan_minute_nine(sample, markets, now):
    """Pure decision: event close matches the mean window, NO ask is 59–91c."""
    start = sample.get("window_start_ms")
    end = sample.get("window_end_ms")
    stamp = sample.get("sample_ms")
    if not all(isinstance(v, int) for v in (start, end, stamp)):
        return None
    if start % 900000 or end != start + 900000:
        return None
    elapsed_ms = now * 1000 - start
    if not 540000 <= elapsed_ms < 600000:  # 9:00–9:59, NEVER minute 10
        return None
    if not start + 540000 <= stamp <= now * 1000 + 1000:
        return None
    if now * 1000 - stamp > 75000:  # no stale, replayed or old-window signals
        return None
    coin = nearest_mean_coin(sample)
    if coin is None:
        return None
    valid = []
    for market in markets:
        ticker = str(market.get("ticker", ""))
        if not ticker.startswith(SERIES + "-") or ticker.rsplit("-", 1)[-1].upper() != coin:
            continue
        if market.get("status") not in {"active", "open"}:
            continue
        try:
            if abs(parse_time(market["close_time"]) * 1000 - end) > 1000:
                continue
            quote = D(str(market["no_ask_dollars"]))
        except (ValueError, KeyError, TypeError, ArithmeticError):
            continue
        if not quote.is_finite() or not MIN_PRICE <= quote <= MAX_PRICE:
            continue
        limit = (quote * 100).to_integral_value(rounding=ROUND_CEILING) / 100
        if limit > MAX_PRICE:
            continue
        valid.append({"ticker": ticker, "coin": coin, "side": "NO",
                      "ask": str(quote), "limit": str(limit),
                      "window_start_ms": start, "window_end_ms": end})
    return valid[0] if len(valid) == 1 else None


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


def collector_sample(session, url, key, now, source_id):
    # The collector stores every sample; read just the last 30 based on its
    # append-only sample count, and validate its original timestamp.
    headers = {"Authorization": "Bearer " + key}
    status = session.get(url + "/status", headers=headers, timeout=4)
    status.raise_for_status()
    count = int(status.json()["samples"])
    if count <= 0:
        return None
    response = session.get(url + "/samples",
        params={"after_id": max(0, count - 30), "limit": 35},
        headers=headers, timeout=4)
    response.raise_for_status()
    samples = response.json().get("samples") or []
    matching = [s for s in samples if s.get("source_id") == source_id
                and s.get("sample_ms", 0) <= now * 1000 + 1000]
    return max(matching, key=lambda item: item["sample_ms"]) if matching else None


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
    if held != -remaining:  # No netting/over-selling any manual inventory
        event("M9_EXIT_OWNERSHIP_WAIT", ticker=ticker, bought=str(bought),
              sold=str(sold), held=str(held))
        return
    competing = [o for o in client.all_orders(ticker, "resting")
                 if o.get("order_id") not in
                 {item.get("order_id") for item in record["sells"]}]
    if competing:
        event("M9_EXIT_OTHER_ORDER_WAIT", ticker=ticker)
        return
    cost = sum((D(str(f.get("count_fp", f.get("count")))) * _outcome_cost(f, -1)
                for f in buy_rows), D(0)) / bought
    target = (cost + PROFIT).quantize(D(".01"), rounding=ROUND_CEILING)
    if target > D(".99"):
        raise RuntimeError("TP above maximum tradable quote")
    bid = D(str(market["no_bid_dollars"]))
    if bid < target:
        return
    intent = {"client_id": str(uuid.uuid4()), "quantity": str(remaining),
              "target": str(target), "created_at": now}
    record["sells"].append(intent)
    journal.save()  # Intent is durable before any POST
    event("M9_TP_SUBMIT", ticker=ticker, quantity=str(remaining), target=str(target))
    response = client.place_take_profit(ticker, -remaining, target,
                                        client_order_id=intent["client_id"])
    row = response.get("order", response)
    if row.get("order_id"):
        intent["order_id"] = row["order_id"]
        journal.save()


def run_pass(client, session, journal, collector_url, read_key, source, quantity, live, now):
    # Exits first so an existing position never depends on a new mean sample.
    for record in list(journal.data["windows"].values()):
        if record.get("buy"):
            try:
                reconcile_exit(client, journal, record, now)
            except Exception as error:
                event("M9_EXIT_RECONCILIATION_PAUSED", ticker=record["ticker"],
                      error_type=type(error).__name__)
    sample = collector_sample(session, collector_url, read_key, now, source)
    if not sample:
        return
    start = sample.get("window_start_ms")
    if not isinstance(start, int) or str(start) in journal.data["windows"]:
        return
    # Cheap early check, but plan_minute_nine is authoritative.
    if not start + 540000 <= now * 1000 < start + 600000:
        return
    markets = client.markets(series_ticker=SERIES, status="open", limit=200)
    plan = plan_minute_nine(sample, markets, now)
    if not plan:
        return
    ticker = plan["ticker"]
    if position_for(client, ticker) != 0 or client.all_orders(ticker, "resting"):
        event("M9_ENTRY_BLOCKED_EXISTING_INVENTORY", ticker=ticker)
        return
    if not live:
        event("M9_DRY_RUN_SIGNAL", **plan, quantity=quantity,
              target_example=str(D(plan["limit"]) + PROFIT))
        return
    # Requote immediately; the 59–91c gate applies to the freshest executable ask.
    market = client.market(ticker)
    current = plan_minute_nine(sample, [market], time.time())
    if not current or current["coin"] != plan["coin"]:
        return
    intent = {"client_id": str(uuid.uuid4()), "limit": current["limit"],
              "created_at": time.time()}
    record = {"ticker": ticker, "coin": plan["coin"], "side": "NO",
              "quantity": str(quantity), "buy": intent, "sells": []}
    journal.data["windows"][str(start)] = record
    journal.save()  # One and only one entry attempt per window, across restarts.
    event("M9_BUY_SUBMIT", ticker=ticker, side="NO", quantity=str(quantity),
          price=current["limit"])
    response = client.place_entry(ticker, "NO", quantity, D(current["limit"]),
                 parse_time(market["close_time"]),
                 submit_before=(start + 600000) / 1000,
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
    url = os.environ.get("M9_COLLECTOR_URL", "").rstrip("/")
    key = os.environ.get("M9_COLLECTOR_READ_KEY", "")
    if not url.startswith(("https://", "http://")) or len(key) < 32:
        raise RuntimeError("M9_COLLECTOR_URL and M9_COLLECTOR_READ_KEY are required")
    source = os.environ.get("M9_SOURCE_ID", "mean-main")
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
        event("M9_READY", live=args.live, source=source, quantity=str(qty),
              price_range_cents="59-91", take_profit_cents=5, no_stop=True)
        while not stop:
            started = time.monotonic()
            try:
                run_pass(client, session, journal, url, key, source, qty, args.live, time.time())
            except Exception as error:
                event("M9_PASS_PAUSED", error_type=type(error).__name__)
            if args.once:
                break
            time.sleep(max(.1, poll - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
