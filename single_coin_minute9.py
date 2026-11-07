"""Single-coin 15-minute Kalshi markets: minute-nine buy, +5c gross exit.

For BTC/ETH/SOL/XRP/HYPE individual UP/DOWN contracts, this strategy buys
whichever side has a quoted ask from 59c through 91c during elapsed minute 9.
No basket mean, leader rank, direction indicator or other coin-selection rule.
Both sides qualifying is ambiguous, so the market is skipped. By default
only the first eligible single-coin market per 15-minute window is entered;
M9_MAX_ENTRIES_PER_WINDOW can increase this limit.

Standalone preview mode by default. Live mode requires --live AND an explicit
M9_TRADING_ENABLED=true setting. DO NOT run alongside another bot that adopts
all outside buys as manual inventory: competing take-profits could oversell.
A fixed 45c executable bid triggers a price-protected reduce-only IOC,
regardless of entry price. The actual exit fill can be below 45c on a gap;
IOC exits require the process to run.
After three confirmed profitable trades, the per-entry budget drops to
one third of the normal allocation until the next confirmed losing trade; then the consecutive-win count resets.
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
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

import requests
from dotenv import load_dotenv

from kalshi import KalshiClient
from price_pairs import _outcome_cost

DEFAULT_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
MIN_PRICE, MAX_PRICE, PROFIT, STOP_BID = D(".59"), D(".91"), D(".05"), D(".45")
TERMINAL = {"executed", "canceled", "cancelled", "expired", "rejected"}
FEE_RESERVE = D(".03")  # conservative per-contract entry fee allowance


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


def exit_decision(cost, bid, *, stop_armed=False):
    """Return (exit kind, protected IOC limit) from executable bid and fill cost.

    The fixed 45-cent bid is the trigger, independent of the actual entry cost:
    on a gap the exit limit is the currently quoted bid, not the stop threshold.
    A latched stop keeps priority over TP through later price recovery/restarts.
    """
    cost, bid = D(str(cost)), D(str(bid))
    if not cost.is_finite() or not D(0) < cost < D(1):
        raise ValueError("Invalid verified entry cost")
    if not bid.is_finite() or not D(0) <= bid <= D(1):
        raise ValueError("Invalid executable bid")
    if stop_armed or bid <= STOP_BID:
        # No executable bid means stop stays armed and is retried next pass.
        return "stop_loss", bid if D(0) < bid < D(1) else None
    target = (cost + PROFIT).quantize(D(".01"), rounding=ROUND_CEILING)
    if target > D(".99"):
        return None, None
    return ("take_profit", target) if bid >= target else (None, None)


def starting_streak():
    return {"consecutive_wins": 0, "reduced_budget": False}


def next_streak(streak, outcome):
    """A win must be confirmed, not a submitted order or an unrealized gain."""
    wins = streak["consecutive_wins"]
    half = streak["reduced_budget"]
    if type(wins) is not int or wins < 0 or wins > 3 or type(half) is not bool:
        raise ValueError("Invalid persisted streak")
    if outcome == "loss":
        return starting_streak()
    if outcome == "flat":
        return {"consecutive_wins": wins if half else 0, "reduced_budget": half}
    if outcome != "win":
        raise ValueError("Only finalized trade results advance the streak")
    if half:
        return {"consecutive_wins": 3, "reduced_budget": True}
    wins = min(3, wins + 1)
    return {"consecutive_wins": wins, "reduced_budget": wins >= 3}


def trade_budget(streak, base_budget):
    """One third of base dollars, not one third of contracts; no compounding."""
    amount = D(str(base_budget))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Trade budget must be positive")
    return amount / D(3) if streak["reduced_budget"] else amount


def budgeted_quantity(budget, ask, cap=100):
    """Protect against exceeding the allocation including fee allowance."""
    amount, price = D(str(budget)), D(str(ask))
    if (not amount.is_finite() or not price.is_finite() or amount <= 0
            or price < MIN_PRICE or price > MAX_PRICE or type(cap) is not int or not 1 <= cap <= 100):
        raise ValueError("Invalid budget, entry price or quantity cap")
    return min(cap, int((amount / (price + FEE_RESERVE)).to_integral_value(rounding=ROUND_FLOOR)))


def apply_new_outcomes(journal):
    """Count each completed trade once, in verified completion-time order."""
    pending = sorted(
        (r for r in journal.data["windows"].values()
         if isinstance(r.get("outcome"), dict) and not r["outcome"].get("counted")),
        key=lambda r: (r["outcome"]["completed_at"], r["ticker"]))
    for record in pending:
        status = record["outcome"]["status"]
        if status not in {"win", "loss", "flat", "no_fill"}:
            raise RuntimeError("Invalid trade outcome; streak not advanced")
        if status != "no_fill":
            journal.data["streak"] = next_streak(journal.data["streak"], status)
        record["outcome"]["counted"] = True
        journal.save()  # atomic outcome + streak update (restart-safe)
        event("M9_TRADE_RESULT", ticker=record["ticker"], result=status,
              net_pnl_dollars=record["outcome"].get("net_pnl_dollars"),
              consecutive_wins=journal.data["streak"]["consecutive_wins"],
              reduced_budget=journal.data["streak"]["reduced_budget"])


def fill_fee(row):
    fee = D(str(row["fee_cost"]))
    if not fee.is_finite() or fee < 0:
        raise ValueError("Invalid exchange-reported fill fee")
    return fee


def fill_timestamp(row):
    if row.get("created_time"):
        return parse_time(row["created_time"])
    if row.get("ts") is not None:
        return float(row["ts"])
    raise ValueError("Cannot establish completion time from fill")


def verify_completed_trade(client, journal, record, now):
    """Only fully reconciled exits or finalized market payouts affect streak."""
    if record.get("outcome") or not record.get("buy"):
        return
    ticker = record["ticker"]
    market = client.market(ticker)
    buy = find_order(client, ticker, record["buy"])
    if buy is None or buy.get("status") not in TERMINAL:
        return
    journal.save()  # persist discovered order ID
    fills = client.all_fills(ticker)
    bought, buy_rows = filled_quantity(fills, record["buy"]["order_id"])
    receipt = buy.get("fill_count_fp", buy.get("fill_count"))
    if receipt is None or D(str(receipt)) != bought:
        return  # exchange read models may have different propagation delays
    if bought > D(record["quantity"]):
        raise RuntimeError("Buy quantity exceeds saved authorization")
    if bought == 0:
        record["outcome"] = {"status": "no_fill", "completed_at": now, "counted": False}
        journal.save()
        return
    valid_order_ids = {record["buy"]["order_id"]}
    sold, sell_rows = D(0), []
    for intent in record["sells"]:
        order = find_order(client, ticker, intent)
        if order is None or order.get("status") not in TERMINAL:
            return
        journal.save()
        count, rows = filled_quantity(fills, intent["order_id"])
        receipt = order.get("fill_count_fp", order.get("fill_count"))
        if receipt is None or D(str(receipt)) != count or count > D(intent["quantity"]):
            return
        sold += count
        sell_rows.extend(rows)
        valid_order_ids.add(intent["order_id"])
    if sold > bought:
        raise RuntimeError("Exit quantity exceeds verified purchases")
    # A manual fill or another worker's order compromises ownership accounting.
    if any(row.get("order_id") not in valid_order_ids for row in fills):
        return
    sign = 1 if record["side"] == "YES" else -1
    remaining = bought - sold
    if remaining:
        if market.get("status") not in {"finalized", "settled"}:
            return
        result = str(market.get("result", "")).lower()
        if result not in {"yes", "no"}:
            return
        payout = D(1) if result == record["side"].lower() else D(0)
        proceeds = payout * remaining
        stamp = market.get("settlement_ts") or market.get("settled_time")
        completed_at = parse_time(stamp) if stamp else parse_time(market["close_time"])
    else:
        proceeds = D(0)
        completed_at = max(fill_timestamp(row) for row in sell_rows)
    # Evaluate the entire entry, including partial TP fills and any settlement
    # payout, using actual exchange fill prices and exact reported fill fees.
    costs = sum((D(str(r.get("count_fp", r.get("count")))) * _outcome_cost(r, sign)
                 for r in buy_rows), D(0))
    proceeds += sum((D(str(r.get("count_fp", r.get("count")))) * _outcome_cost(r, sign)
                     for r in sell_rows), D(0))
    fees = sum((fill_fee(r) for r in buy_rows + sell_rows), D(0))
    pnl = proceeds - costs - fees
    status = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
    record["outcome"] = {"status": status, "net_pnl_dollars": str(pnl),
                          "completed_at": completed_at, "counted": False,
                          "bought": str(bought), "sold": str(sold),
                          "remaining_at_settlement": str(remaining)}
    journal.save()


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            obj = json.loads(self.path.read_text())
            if obj.get("schema") != 1 or not isinstance(obj.get("windows"), dict):
                raise RuntimeError("Invalid minute-9 journal; manual recovery required")
            self.data = obj
            if "streak" not in self.data:
                self.data["streak"] = starting_streak()
                self.save()
            else:
                saved_streak = self.data["streak"]
                if "half_budget" in saved_streak and "reduced_budget" not in saved_streak:
                    saved_streak["reduced_budget"] = saved_streak.pop("half_budget")
                    for record in self.data["windows"].values():
                        if "half_budget_at_entry" in record and "reduced_budget_at_entry" not in record:
                            record["reduced_budget_at_entry"] = record.pop("half_budget_at_entry")
                    self.save()
                next_streak(saved_streak, "flat")  # validate before trading
        else:
            self.data = {"schema": 1, "windows": {}, "streak": starting_streak()}
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
    bid = D(str(market[record["side"].lower() + "_bid_dollars"]))
    purpose, limit = exit_decision(cost, bid, stop_armed=bool(record.get("stop_armed")))
    if purpose is None:
        return
    if purpose == "stop_loss" and not record.get("stop_armed"):
        # Latch the trigger even if the book temporarily loses its best bid.
        record["stop_armed"] = {
            "at": now, "trigger_bid": str(bid), "threshold": str(STOP_BID)
        }
        journal.save()
        event("M9_STOP_TRIGGERED", ticker=ticker, entry_cost=str(cost),
              trigger_bid=str(bid), threshold=str(STOP_BID))
    if limit is None:
        event("M9_STOP_NO_EXECUTABLE_BID", ticker=ticker)
        return
    intent = {"client_id": str(uuid.uuid4()), "quantity": str(remaining),
              "target": str(limit), "purpose": purpose, "created_at": now}
    record["sells"].append(intent)
    journal.save()  # Intent is durable before any POST
    event("M9_STOP_SUBMIT" if purpose == "stop_loss" else "M9_TP_SUBMIT",
          ticker=ticker, quantity=str(remaining), target=str(limit),
          entry_cost=str(cost), trigger_bid=str(bid), execution="reduce_only_ioc")
    response = client.place_take_profit(ticker, sign * remaining, limit,
                                        client_order_id=intent["client_id"])
    row = response.get("order", response)
    if row.get("order_id"):
        intent["order_id"] = row["order_id"]
        journal.save()


def window_entry_count(journal, window_start_ms):
    """Count durable attempts across every configured single-coin market."""
    if type(window_start_ms) is not int or window_start_ms % 900000:
        raise ValueError("Invalid 15-minute window start")
    return sum(1 for record in journal.data["windows"].values()
               if record.get("window_start_ms") == window_start_ms
               and isinstance(record.get("buy"), dict))


def run_pass(client, journal, series_list, base_budget, max_contracts, live, now,
             max_entries_per_window=1):
    # Check exits first. Never require TradingView or a mean data collector.
    for record in list(journal.data["windows"].values()):
        if record.get("buy") and not record.get("outcome"):
            try:
                reconcile_exit(client, journal, record, now)
                verify_completed_trade(client, journal, record, now)
            except Exception as error:
                event("M9_EXIT_OR_RESULT_RECONCILIATION_PAUSED", ticker=record["ticker"],
                      error_type=type(error).__name__)
    apply_new_outcomes(journal)
    # Only one entry per quarter hour matches the 96-trade/day illustration;
    # selecting the first price-eligible individual coin is not a basket signal.
    # Preview does not write attempts, so deduplicate its signals in memory.
    preview_windows = set()
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
            start_ms = plan["window_start_ms"]
            if (window_entry_count(journal, start_ms) >= max_entries_per_window
                    or (not live and start_ms in preview_windows)):
                continue  # No overtrading when five coins qualify in one period.
            if ticker in journal.data["windows"]:
                continue  # One attempt per individual coin market and 15m window.
            if position_for(client, ticker) != 0 or client.all_orders(ticker, "resting"):
                event("M9_ENTRY_BLOCKED_EXISTING_INVENTORY", ticker=ticker)
                continue
            allocation = trade_budget(journal.data["streak"], base_budget)
            quantity = budgeted_quantity(allocation, D(plan["limit"]), max_contracts)
            if quantity < 1:
                event("M9_ENTRY_BUDGET_TOO_SMALL", ticker=ticker,
                      allocated_dollars=str(allocation), limit=plan["limit"])
                continue
            if not live:
                preview_windows.add(start_ms)
                event("M9_DRY_RUN_SIGNAL", **plan, quantity=quantity,
                      allocated_dollars=str(allocation),
                      reduced_budget=journal.data["streak"]["reduced_budget"],
                      consecutive_wins=journal.data["streak"]["consecutive_wins"],
                      target_example=str(D(plan["limit"]) + PROFIT))
                continue
            # Fresh quote, same side, same 15-minute market, and still minute 9.
            market = client.market(ticker)
            current = plan_single_coin(market, time.time(), series)
            if current is None or current["side"] != plan["side"]:
                continue
            quantity = budgeted_quantity(allocation, D(current["limit"]), max_contracts)
            if quantity < 1:
                continue
            intent = {"client_id": str(uuid.uuid4()), "limit": current["limit"],
                      "created_at": time.time()}
            record = {"ticker": ticker, "series": series, "side": current["side"],
                      "window_start_ms": current["window_start_ms"],
                      "quantity": str(quantity), "allocated_dollars": str(allocation),
                      "reduced_budget_at_entry": journal.data["streak"]["reduced_budget"],
                      "buy": intent, "sells": []}
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
    if "M9_QUANTITY" in os.environ:
        raise ValueError("M9_QUANTITY is retired; configure M9_TRADE_BUDGET_DOLLARS instead")
    base_budget = D(os.getenv("M9_TRADE_BUDGET_DOLLARS",
                            os.getenv("MARKET_BUDGET_DOLLARS", "20")))
    if not base_budget.is_finite() or not D("1") <= base_budget <= D("1000"):
        raise ValueError("M9_TRADE_BUDGET_DOLLARS must be $1–$1000")
    max_contracts = int(os.getenv("M9_MAX_CONTRACTS", "100"))
    if not 1 <= max_contracts <= 100:
        raise ValueError("M9_MAX_CONTRACTS must be between 1 and 100")
    poll = float(os.getenv("M9_POLL_SECONDS", "2"))
    if not 1 <= poll <= 30:
        raise ValueError("M9_POLL_SECONDS must be between 1 and 30")
    series_list = tuple(s.strip().upper() for s in os.getenv("M9_SERIES", ",".join(DEFAULT_SERIES)).split(",") if s.strip())
    if not series_list or len(set(series_list)) != len(series_list) or any(s not in DEFAULT_SERIES for s in series_list):
        raise ValueError("M9_SERIES must be a unique subset of BTC, ETH, SOL, XRP, HYPE 15m series")
    max_entries_per_window = int(os.getenv("M9_MAX_ENTRIES_PER_WINDOW", "1"))
    if not 1 <= max_entries_per_window <= len(series_list):
        raise ValueError("M9_MAX_ENTRIES_PER_WINDOW must be between 1 and the enabled series count")
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
        event("M9_READY", live=args.live, series=series_list,
              base_budget_dollars=str(base_budget), max_contracts=max_contracts,
              max_entries_per_window=max_entries_per_window,
              consecutive_wins=journal.data["streak"]["consecutive_wins"],
              reduced_budget=journal.data["streak"]["reduced_budget"],
              price_range_cents="59-91", take_profit_cents=5,
              stop_bid_cents=int(STOP_BID * 100), stop_execution="bid_limited_reduce_only_ioc")
        while not stop:
            started = time.monotonic()
            try:
                run_pass(client, journal, series_list, base_budget, max_contracts,
                         args.live, time.time(), max_entries_per_window)
            except Exception as error:
                event("M9_PASS_PAUSED", error_type=type(error).__name__)
            if args.once:
                break
            time.sleep(max(.1, poll - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
