"""Single-coin 15-minute Kalshi markets: minute-nine buy, +5c gross exit.

For BTC/ETH/SOL/XRP/HYPE individual UP/DOWN contracts, this strategy buys
whichever side has a quoted ask from 69c through 91c from elapsed minute 9 until the configured entry cutoff.
No basket mean, leader rank, direction indicator or other coin-selection rule.
Both sides qualifying is ambiguous, so the market is skipped. By default
only one filled or potentially-filled market per 15-minute window is allowed.
Candidates keep M9_SERIES order. A priced-out candidate advances to the next;
a proven terminal zero-fill entry releases its slot for the backup candidate.
Partial fills and unknown acknowledgements NEVER release the window slot.
M9_MAX_ENTRIES_PER_WINDOW can increase the filled/pending-market limit.
Only orders matched to this runner's saved client/order IDs are managed.
Outside positions and orders are left untouched; overlapping markets are skipped.

Set M9_ENTRY_END_SECONDS=900 for entries from minute 9 until market close;
the unset default of 600 preserves the archived minute-nine-only mode.
Standalone preview mode by default. Live mode requires --live AND an explicit
M9_TRADING_ENABLED=true setting. DO NOT run alongside another bot that adopts
all outside buys as manual inventory: competing take-profits could oversell.
An absolute executable bid at M9_STOP_BID_CENTS (default 45c) triggers a price-protected reduce-only IOC,
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

from kalshi import KalshiClient, KalshiAPIError
from price_pairs import _outcome_cost

DEFAULT_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXHYPE15M")
MIN_PRICE, RELAXED_MIN_PRICE, MAX_PRICE, PROFIT = (
    D(".69"), D(".60"), D(".91"), D(".05")
)


def stop_bid_from_cents(value):
    """Strict integer-cent stop threshold, independent of entry price."""
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or not 1 <= int(value) <= 99):
        raise ValueError("M9_STOP_BID_CENTS must be an integer from 1 to 99")
    return D(value) / D("100")


STOP_BID = stop_bid_from_cents(os.getenv("M9_STOP_BID_CENTS", "45"))
RELAX_FLOOR_AFTER_WINS = 10
TERMINAL = {"executed", "canceled", "cancelled", "expired", "rejected"}
FEE_RESERVE = D(".03")  # conservative per-contract entry fee allowance
ENTRY_QUEUE_POLICY = "confirmed-zero-fill-fallback-v1"
ENTRY_START_SECONDS = 540
ENTRY_END_SECONDS = int(os.getenv("M9_ENTRY_END_SECONDS", "600"))
if not 600 <= ENTRY_END_SECONDS <= 900:
    raise ValueError("M9_ENTRY_END_SECONDS must be between 600 and 900")


def event(name, **details):
    print(json.dumps({"event": name, "at": datetime.now(timezone.utc).isoformat(),
                      **details}, default=str), flush=True)


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Venue timestamps must include a timezone")
    return parsed.timestamp()


def plan_single_coin(market, now, series, *, min_price=None):
    """Return an eligible YES/NO ask using the active streak-dependent floor."""
    min_price = MIN_PRICE if min_price is None else D(str(min_price))
    if not min_price.is_finite() or not RELAXED_MIN_PRICE <= min_price <= MAX_PRICE:
        raise ValueError("Invalid entry minimum")
    ticker = str(market.get("ticker", ""))
    if not ticker.startswith(series + "-") or market.get("status") not in {"active", "open"}:
        return None
    try:
        close = parse_time(market["close_time"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    # Each 15-minute series settles a fixed 900s after the contract window opens.
    open_at = close - 900
    if not ENTRY_START_SECONDS <= now - open_at < ENTRY_END_SECONDS:
        return None
    candidates = []
    for side in ("YES", "NO"):
        try:
            ask = D(str(market[side.lower() + "_ask_dollars"]))
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        if not ask.is_finite() or ask < min_price or ask > MAX_PRICE:
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

    The configured absolute stop bid is the trigger, independent of entry cost:
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
    """Track true consecutive wins while preserving the three-win budget rule."""
    wins = streak["consecutive_wins"]
    reduced = streak["reduced_budget"]
    if (type(wins) is not int or wins < 0 or wins > RELAX_FLOOR_AFTER_WINS
            or type(reduced) is not bool):
        raise ValueError("Invalid persisted streak")
    if outcome == "loss":
        return starting_streak()
    if outcome == "flat":
        # A flat is not a win, so it breaks the 10-win floor-unlock streak.
        # Reduced-budget mode, once earned, remains until a confirmed loss.
        return {"consecutive_wins": 0, "reduced_budget": reduced}
    if outcome != "win":
        raise ValueError("Only finalized trade results advance the streak")
    wins = min(RELAX_FLOOR_AFTER_WINS, wins + 1)
    return {"consecutive_wins": wins, "reduced_budget": reduced or wins >= 3}


def entry_min_price(streak):
    """69c normally; unlock a 60c floor after ten consecutive confirmed wins."""
    # Validate using the same persisted-state contract as next_streak.
    next_streak(streak, "flat")
    return RELAXED_MIN_PRICE if streak["consecutive_wins"] >= RELAX_FLOOR_AFTER_WINS else MIN_PRICE


def trade_budget(streak, base_budget):
    """One third of base dollars, not one third of contracts; no compounding."""
    amount = D(str(base_budget))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Trade budget must be positive")
    return amount / D(3) if streak["reduced_budget"] else amount


def budgeted_quantity(budget, ask, cap=100, *, min_price=None):
    """Protect against exceeding the allocation including fee allowance."""
    amount, price = D(str(budget)), D(str(ask))
    min_price = MIN_PRICE if min_price is None else D(str(min_price))
    if (not amount.is_finite() or not price.is_finite() or amount <= 0
            or not min_price.is_finite() or min_price < RELAXED_MIN_PRICE
            or min_price > MAX_PRICE or price < min_price or price > MAX_PRICE
            or type(cap) is not int or not 1 <= cap <= 100):
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
              reduced_budget=journal.data["streak"]["reduced_budget"],
              entry_floor_cents=int(entry_min_price(journal.data["streak"]) * 100))


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
        # Release a window slot only after the exact owned order is terminal,
        # both counts are explicitly zero, and no inventory/order can still fill.
        # Missing counts or an HTTP error are NOT proof of zero execution.
        remaining_count = buy.get("remaining_count_fp", buy.get("remaining_count"))
        if (buy.get("status") not in {"canceled", "cancelled", "expired", "rejected"}
                or remaining_count is None or D(str(remaining_count)) != 0
                or buy.get("subaccount_number", 0) not in {0, None}
                or buy_rows or record.get("sells")):
            return
        if position_for(client, ticker) != 0 or client.all_orders(ticker, "resting"):
            return
        record["entry_zero_fill_verified"] = {
            "order_id": buy["order_id"], "client_id": record["buy"]["client_id"],
            "status": buy["status"], "filled": "0", "remaining": "0",
            "checked_at": now,
        }
        record["outcome"] = {"status": "no_fill", "completed_at": now, "counted": False}
        journal.save()  # Proof and classification are one atomic journal write.
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
    """Resolve only the exact order identity recorded before this bot's POST."""
    expected_client_id = intent.get("client_id")
    if not isinstance(expected_client_id, str) or not expected_client_id:
        raise RuntimeError("Missing saved order ownership identity")
    if intent.get("order_id"):
        try:
            order = client.order(intent["order_id"], ticker)
        except KalshiAPIError as error:
            if error.status_code != 404:
                raise
            # The direct read and exact-ID history fallback have not caught up.
            # Retain the durable intent and wait; never infer a zero fill.
            event("M9_ORDER_VISIBILITY_PENDING", ticker=ticker,
                  http_status=404, order_id=intent["order_id"])
            return None
        if (order.get("ticker") != ticker
                or order.get("order_id") != intent["order_id"]
                or order.get("client_order_id") != expected_client_id):
            raise RuntimeError("Order ownership mismatch")
        return order
    # Lost acknowledgement: reconcile by durable client_order_id. Never repeat POST.
    matching = [row for row in client.all_orders(ticker)
                if row.get("client_order_id") == intent["client_id"]]
    if len(matching) > 1:
        raise RuntimeError("Multiple orders matched one client id")
    if matching:
        if matching[0].get("ticker") != ticker or not matching[0].get("order_id"):
            raise RuntimeError("Order ownership mismatch")
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


def entry_market_occupied(client, ticker):
    """New entries need an untouched market; never adopt outside activity."""
    return bool(position_for(client, ticker) != 0
                or client.all_orders(ticker, "resting")
                or client.all_fills(ticker))


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
    receipt = buy.get("fill_count_fp", buy.get("fill_count"))
    if receipt is None or D(str(receipt)) != bought:
        event("M9_BUY_FILL_RECONCILING", ticker=ticker)
        return
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
    owned_ids = {record["buy"]["order_id"]} | {
        intent["order_id"] for intent in record["sells"]
    }
    if any(row.get("order_id") not in owned_ids for row in all_fills):
        # Account positions are netted. Do not assume matching net quantity
        # proves ownership after an outside trade on this same contract.
        event("M9_EXTERNAL_ACTIVITY_SKIPPED", ticker=ticker)
        return
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


def entry_slot_released(record):
    """Only this runner's saved, verified zero-fill proof releases a slot."""
    proof = record.get("entry_zero_fill_verified") or {}
    intent = record.get("buy") or {}
    return bool(record.get("outcome", {}).get("status") == "no_fill"
                and intent.get("order_id") and intent.get("client_id")
                and proof.get("order_id") == intent["order_id"]
                and proof.get("client_id") == intent["client_id"]
                and proof.get("status") in {"canceled", "cancelled", "expired", "rejected"}
                and proof.get("filled") == "0" and proof.get("remaining") == "0"
                and not record.get("sells"))


def window_entry_count(journal, window_start_ms):
    """Count filled or potentially-filled entries, not proven zero-fill misses."""
    if type(window_start_ms) is not int or window_start_ms % 900000:
        raise ValueError("Invalid 15-minute window start")
    return sum(1 for record in journal.data["windows"].values()
               if record.get("window_start_ms") == window_start_ms
               and isinstance(record.get("buy"), dict)
               and not entry_slot_released(record))



def queue_skip(client_market, now, series, *, min_price=None):
    """Explain in-window quote skips; do not spam logs for future markets."""
    min_price = MIN_PRICE if min_price is None else D(str(min_price))
    try:
        start = parse_time(client_market["close_time"]) - 900
        if not ENTRY_START_SECONDS <= now - start < ENTRY_END_SECONDS:
            return
    except (KeyError, TypeError, ValueError, AttributeError):
        return
    asks = {}
    for side in ("yes", "no"):
        try:
            value = D(str(client_market[side + "_ask_dollars"]))
            asks[side] = str(value) if value.is_finite() else None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            asks[side] = None
    eligible = [v for v in asks.values() if v is not None and min_price <= D(v) <= MAX_PRICE]
    reason = ("market_not_active" if client_market.get("status") not in {"active", "open"}
              else "ambiguous_sides" if len(eligible) > 1
              else "missing_quote" if None in asks.values()
              else f"outside_{int(min_price * 100)}_{int(MAX_PRICE * 100)}_price_band")
    event("M9_QUEUE_SKIP", ticker=client_market.get("ticker"), series=series,
          reason=reason, yes_ask=asks["yes"], no_ask=asks["no"])


def candidate_queue(client, series_list, now, *, min_price=None):
    """Price-eligible single coins in configured order, not basket ranking."""
    min_price = MIN_PRICE if min_price is None else D(str(min_price))
    setter = getattr(client, "set_entry_min_price", None)
    if callable(setter):
        setter(min_price)
    candidates = []
    for series in series_list:
        try:
            markets = client.markets(series_ticker=series, status="open", limit=100)
        except Exception as error:
            event("M9_MARKET_DISCOVERY_PAUSED", series=series, error_type=type(error).__name__)
            continue
        for market in markets:
            plan = plan_single_coin(market, now, series, min_price=min_price)
            if plan is None:
                queue_skip(market, now, series, min_price=min_price)
            else:
                candidates.append(plan)
    return candidates

def run_pass(client, journal, series_list, base_budget, max_contracts, live, now,
             max_entries_per_window=1):
    # Preview must never send exits, even if pointed at an existing live journal.
    # Live exits operate only on this runner's durable order identities.
    if live:
        for record in list(journal.data["windows"].values()):
            if record.get("buy") and not record.get("outcome"):
                try:
                    reconcile_exit(client, journal, record, now)
                    verify_completed_trade(client, journal, record, now)
                except Exception as error:
                    event("M9_EXIT_OR_RESULT_RECONCILIATION_PAUSED", ticker=record["ticker"],
                          error_type=type(error).__name__)
        apply_new_outcomes(journal)
    # Queue order is unchanged (M9_SERIES). Refresh each candidate before use;
    # never reuse a backup's stale snapshot as an executable quote.
    preview_windows = set()
    min_price = entry_min_price(journal.data["streak"])
    candidates = candidate_queue(client, series_list, now, min_price=min_price)
    if candidates:
        event("M9_ENTRY_QUEUE", tickers=[p["ticker"] for p in candidates],
              policy=ENTRY_QUEUE_POLICY)
    for plan in candidates:
        ticker, series = plan["ticker"], plan["series"]
        start_ms = plan["window_start_ms"]
        if (window_entry_count(journal, start_ms) >= max_entries_per_window
                or (not live and start_ms in preview_windows)):
            continue
        if ticker in journal.data["windows"]:
            continue  # Never retry the same coin: move to its queued backup.
        if entry_market_occupied(client, ticker):
            event("M9_ENTRY_BLOCKED_EXISTING_INVENTORY", ticker=ticker)
            continue
        allocation = trade_budget(journal.data["streak"], base_budget)
        quantity = budgeted_quantity(allocation, D(plan["limit"]), max_contracts,
                                     min_price=min_price)
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
                  entry_floor_cents=int(min_price * 100),
                  target_example=str(D(plan["limit"]) + PROFIT))
            continue
        market = client.market(ticker)
        current = plan_single_coin(market, time.time(), series, min_price=min_price)
        if (current is None or current["side"] != plan["side"]
                or current["window_start_ms"] != start_ms):
            queue_skip(market, time.time(), series, min_price=min_price)
            event("M9_QUEUE_ADVANCE", ticker=ticker, reason="fresh_quote_not_eligible")
            continue
        quantity = budgeted_quantity(allocation, D(current["limit"]), max_contracts,
                                     min_price=min_price)
        if quantity < 1:
            continue
        if entry_market_occupied(client, ticker):
            event("M9_ENTRY_BLOCKED_EXISTING_INVENTORY", ticker=ticker)
            continue
        # Portfolio checks may have taken time. Never extend the entry cutoff.
        if not start_ms / 1000 + ENTRY_START_SECONDS <= time.time() < start_ms / 1000 + ENTRY_END_SECONDS:
            event("M9_QUEUE_ADVANCE", ticker=ticker, reason="entry_window_ended")
            continue
        intent = {"client_id": str(uuid.uuid4()), "limit": current["limit"],
                  "created_at": time.time()}
        record = {"ticker": ticker, "series": series, "side": current["side"],
                  "window_start_ms": start_ms,
                  "quantity": str(quantity), "allocated_dollars": str(allocation),
                  "reduced_budget_at_entry": journal.data["streak"]["reduced_budget"],
                  "entry_floor_at_entry": str(min_price),
                  "buy": intent, "sells": []}
        journal.data["windows"][ticker] = record
        journal.save()  # A pending/ambiguous POST consumes the slot durably.
        event("M9_BUY_SUBMIT", ticker=ticker, side=current["side"],
              quantity=str(quantity), price=current["limit"],
              entry_floor_cents=int(min_price * 100),
              take_profit_cents=int((D(current["limit"]) + PROFIT) * 100))
        try:
            response = client.place_entry(ticker, current["side"], quantity, D(current["limit"]),
                parse_time(market["close_time"]), submit_before=start_ms / 1000 + ENTRY_END_SECONDS,
                client_order_id=intent["client_id"], ioc=True)
        except Exception as error:
            # Log structured diagnostics, not raw responses or credentials.
            code = getattr(error, "code", None)
            safe_code = code if isinstance(code, str) and len(code) <= 80 and all(
                ch.isalnum() or ch in "_-" for ch in code) else None
            status = getattr(error, "status_code", None)
            event("M9_BUY_SUBMISSION_ERROR", ticker=ticker,
                  error_type=type(error).__name__,
                  http_status=status if type(status) is int else None, api_code=safe_code)
            raise  # Unknown acceptance is never permission to try another coin.
        row = response.get("order", response)
        if row.get("order_id"):
            intent["order_id"] = row["order_id"]
            journal.save()
            try:
                # Do not infer a miss from a quote or a missing acknowledgement.
                # Only an exact terminal-order read + fills can release this slot.
                verify_completed_trade(client, journal, record, time.time())
            except Exception as error:
                event("M9_ENTRY_CONFIRMATION_PENDING", ticker=ticker,
                      error_type=type(error).__name__)
        if entry_slot_released(record):
            event("M9_QUEUE_ADVANCE", ticker=ticker, reason="confirmed_zero_fill")
            apply_new_outcomes(journal)  # no_fill never changes the win streak
        # On any positive fill or unresolved order the count still blocks backups.


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
              entry_queue_policy=ENTRY_QUEUE_POLICY,
              entry_start_seconds=ENTRY_START_SECONDS,
              entry_end_seconds=ENTRY_END_SECONDS,
              consecutive_wins=journal.data["streak"]["consecutive_wins"],
              reduced_budget=journal.data["streak"]["reduced_budget"],
              price_range_cents=f"{int(entry_min_price(journal.data['streak']) * 100)}-{int(MAX_PRICE * 100)}",
              floor_relaxes_after_consecutive_wins=RELAX_FLOOR_AFTER_WINS,
              relaxed_floor_cents=int(RELAXED_MIN_PRICE * 100), take_profit_cents=5,
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
