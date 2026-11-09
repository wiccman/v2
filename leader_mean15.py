"""Crypto Leader 15-minute NO-on-closest-to-mean strategy.

Separate owned-order journal. It does not amend the individual-coin strategy,
reprice another bot's exits, or trade from stale/incomplete coin data.
This uses a fresh five-coin Coinbase window-return mean, NOT the currently
stale TradingView collector. A live opt-in is required independently.
Filled Leader contracts are held through final market settlement, not sold
at minute 14, and only after verified settlement does the independent budget
cycle advance. Other single-coin TP/SL strategies are unaffected.
"""
from __future__ import annotations

import math
import os
import time
import uuid
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from cash_checked_minute9 import cash_snapshot

SERIES = "KXCRYPTOLEAD15M"
COINS = {"BTC": "KXBTC15M", "ETH": "KXETH15M",
         "SOL": "KXSOL15M", "XRP": "KXXRP15M", "HYPE": "KXHYPE15M"}
SOURCE_SERIES = tuple(COINS.values())
TRADE_COINS = ("BTC", "ETH", "XRP", "HYPE")  # SOL only excluded from Leader entries.
ENTRY_SECOND = 180
ENTRY_END_SECOND = 840  # Still stop buying at minute 14.
WINDOW_SECONDS = 900
HOLD_UNTIL_SETTLEMENT = True  # Never initiate an early Leader sell / TP / SL.
MIN_NO_ASK = D(".65")
MAX_NO_LIMIT = D(".99")
FULL_BUDGET = D("10")
HALF_BUDGET = D("5")
FEE_RESERVE = D(".03")
MAX_CONTRACTS = 100
REFRESH_SECONDS = 15
TERMINAL = core.TERMINAL


def event(name, **data):
    core.event("LEADER_" + name, **data)


def eligible_window(now, start, lower, upper):
    return (math.isfinite(now) and type(start) is int and start % WINDOW_SECONDS == 0
            and lower <= now - start < upper)


def coin_mean_choice(observations, start, now):
    """Require five fresh same-window returns; do not promote incomplete feeds."""
    rows = ranked.ranked_observations(observations, SOURCE_SERIES, start, now)
    returns = {name: row.return_pct for name, series in COINS.items()
               for row in rows if row.series == series}
    if len(returns) != len(COINS):
        raise ranked.PerformanceUnavailable("incomplete_leader_mean")
    mean = sum(returns.values(), D(0)) / D(len(COINS))
    choices = sorted(((abs(returns[coin] - mean), coin)
                      for coin in TRADE_COINS))
    if len(choices) < 2 or choices[0][0] == choices[1][0]:
        raise ranked.PerformanceUnavailable("ambiguous_closest_coin")
    return choices[0][1], mean, returns


def leader_plan(markets, coin, start, now):
    """Only an exact current-window NO coin leg, never an UP/DOWN market."""
    if coin not in TRADE_COINS or not eligible_window(now, start, ENTRY_SECOND, ENTRY_END_SECOND):
        return None
    selected = []
    for market in markets:
        ticker = market.get("ticker", "")
        if (not isinstance(ticker, str)
                or not ticker.startswith(SERIES + "-")
                or ticker.rsplit("-", 1)[-1] != coin
                or market.get("status") not in {"active", "open"}):
            continue
        try:
            close = core.parse_time(market["close_time"])
            ask = D(str(market["no_ask_dollars"]))
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            continue
        limit = (ask * D(100)).to_integral_value(rounding=ROUND_CEILING) / 100
        if (close == start + WINDOW_SECONDS and ask.is_finite()
                and MIN_NO_ASK <= ask < D(1) and limit <= MAX_NO_LIMIT):
            selected.append({"ticker": ticker, "coin": coin, "ask": ask,
                             "limit": limit, "close": close})
    return selected[0] if len(selected) == 1 else None


def budget(journal):
    """First $10, then $5 until the next verified loss; no-fill is neutral."""
    unfinished = [r for r in journal.data["windows"].values()
                  if r.get("buy") and not r.get("outcome")]
    if unfinished:
        return None
    finalized = sorted(((r["outcome"]["completed_at"], r.get("ticker", ""),
                         r["outcome"]["status"])
                        for r in journal.data["windows"].values()
                        if r.get("outcome", {}).get("status") in {"win", "loss", "flat"}))
    return FULL_BUDGET if not finalized or finalized[-1][2] == "loss" else HALF_BUDGET


def _price(value):
    amount = D(str(value))
    if not amount.is_finite() or not D(0) <= amount <= D(1):
        raise ValueError("invalid_quote")
    return amount


def _quantity(budget_limit, limit, cash):
    unit = limit + FEE_RESERVE
    return min(MAX_CONTRACTS, int((min(budget_limit, cash) / unit)
                                 .to_integral_value(rounding=ROUND_FLOOR)))


def _owned_fills(client, record):
    ticker = record["ticker"]
    buy = core.find_order(client, ticker, record["buy"])
    if buy is None:
        event("BUY_ACK_UNRESOLVED", ticker=ticker)
        return None
    record["_buy_status_snapshot"] = buy.get("status")
    if buy.get("status") not in TERMINAL:
        return None
    rows = client.all_fills(ticker)
    purchased, buys = core.filled_quantity(rows, record["buy"]["order_id"])
    receipt = buy.get("fill_count_fp", buy.get("fill_count"))
    if receipt is None or _price_count(receipt) != purchased or purchased > D(record["quantity"]):
        event("BUY_RECONCILING", ticker=ticker)
        return None
    return purchased, buys, rows


def _price_count(value):
    result = D(str(value))
    if not result.is_finite() or result < 0 or result != result.quantize(D(".01")):
        raise RuntimeError("invalid_exchange_count")
    return result


def _close_result(client, journal, record, now, bought, buys, sold, sells):
    ticker, side = record["ticker"], record["side"]
    remaining = bought - sold
    if remaining < 0:
        raise RuntimeError("sell_exceeds_verified_buy")
    market = client.market(ticker) if remaining else None
    if remaining and (market.get("status") not in {"finalized", "settled"}
                      or str(market.get("result", "")).lower() not in {"yes", "no"}):
        return False
    proceeds = sum((core._outcome_cost(f, -1) * core.fill_contract_quantity(f)
                    for f in sells), D(0))
    if remaining:
        proceeds += remaining * (D(1) if market["result"].lower() == side.lower() else D(0))
    costs = sum((core._outcome_cost(f, -1) * core.fill_contract_quantity(f)
                 for f in buys), D(0))
    fee = sum((core.fill_fee(f) for f in buys + sells), D(0))
    net = proceeds - costs - fee
    outcome = "win" if net > 0 else "loss" if net < 0 else "flat"
    record["outcome"] = {"status": outcome, "completed_at": now,
                         "net_pnl_dollars": str(net), "bought": str(bought),
                         "sold": str(sold), "settled_remainder": str(remaining)}
    journal.save()
    event("RESULT", ticker=ticker, result=outcome, pnl=str(net),
          bought=str(bought), sold=str(sold), settled_remainder=str(remaining))
    return True


def reconcile(client, journal, record, now):
    """Never repeat an uncertain POST. Old sells are audited, but no new Leader sell."""
    if record.get("outcome"):
        return
    ticker = record["ticker"]
    proof = _owned_fills(client, record)
    if proof is None:
        # IOC should not rest. Cancel only a verified identified live order's
        # unfilled remainder after 120 seconds; never cancel by ticker alone.
        if now - record["buy"]["created_at"] >= 120:
            buy = core.find_order(client, ticker, record["buy"])
            if buy and buy.get("status") not in TERMINAL and record["buy"].get("order_id"):
                last = record.get("cancel_requested_at", -float("inf"))
                if now - last >= 15:
                    record["cancel_requested_at"] = now
                    journal.save()
                    client.cancel(record["buy"]["order_id"], ticker)
                    event("BUY_CANCEL_REQUESTED", ticker=ticker,
                          order_id=record["buy"]["order_id"])
        journal.save()  # Persist any newly resolved owned order ID.
        return
    bought, buys, rows = proof
    journal.save()
    if bought == 0:
        record["outcome"] = {"status": "no_fill", "completed_at": now}
        journal.save()
        event("ZERO_FILL", ticker=ticker)
        return
    sold, sales = D(0), []
    owned_ids = {record["buy"]["order_id"]}
    for intent in record["sells"]:
        order = core.find_order(client, ticker, intent)
        if order is None or order.get("status") not in TERMINAL:
            journal.save()
            event("SELL_RECONCILING", ticker=ticker)
            return
        count, fills = core.filled_quantity(rows, intent["order_id"])
        receipt = order.get("fill_count_fp", order.get("fill_count"))
        if (receipt is None or _price_count(receipt) != count
                or count > D(intent["quantity"])):
            event("SELL_FILL_RECONCILING", ticker=ticker)
            return
        sold += count
        sales.extend(fills)
        owned_ids.add(intent["order_id"])
    if sold > bought:
        raise RuntimeError("oversold")
    if any(row.get("order_id") not in owned_ids for row in rows):
        event("EXTERNAL_ACTIVITY_WAIT", ticker=ticker)
        return
    remaining = bought - sold
    if remaining == 0:
        _close_result(client, journal, record, now, bought, buys, sold, sales)
        return
    if now >= record["close_at"]:
        _close_result(client, journal, record, now, bought, buys, sold, sales)
        return
    # NO forced minute-14 exit: waiting for Kalshi settlement avoids paying
    # the last-minute bid/ask spread to unwind an otherwise winning prediction.
    # Any pre-upgrade sell intents above still reconcile by exact order ID.
    # New Leader positions are not offered to the bid, stopped, or TP-sold.
    return


class LeaderRunner:
    def __init__(self, path, *, provider=None, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self.provider = provider or ranked.CoinbasePerformance(clock=self.clock)
        self.journal = core.Journal(self.path)
        self.next_scan = float("-inf")

    def run(self, client, *, live):
        # A preview must never send orders or exits, even with live journal data.
        if not live:
            return
        now = self.clock()
        for record in list(self.journal.data["windows"].values()):
            if record.get("buy") and not record.get("outcome"):
                try:
                    reconcile(client, self.journal, record, self.clock())
                except Exception as error:
                    event("RECONCILE_WAIT", ticker=record.get("ticker"),
                          error_type=type(error).__name__)
        # Never initiate new entries during incomplete prior fills/settlements.
        now = self.clock()
        start = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
        if not eligible_window(now, start, ENTRY_SECOND, ENTRY_END_SECOND):
            return
        if str(start) in self.journal.data["windows"]:
            return
        if budget(self.journal) is None:
            return
        if now < self.next_scan:
            return
        self.next_scan = now + REFRESH_SECONDS
        try:
            observations = self.provider.snapshot(SOURCE_SERIES, start)
            coin, mean, returns = coin_mean_choice(observations, start, self.clock())
            event("SIGNAL", selected_coin=coin, mean_return_pct=str(mean),
                  returns_pct={k: str(v) for k,v in returns.items()},
                  signal_source="fresh_coinbase_window_returns")
            markets = client.markets(series_ticker=SERIES, status="open", limit=100)
            plan = leader_plan(markets, coin, start, self.clock())
            if plan is None:
                # Public quotes and timestamps only; never log account credentials.
                matching = [m for m in markets
                            if isinstance(m.get("ticker"), str)
                            and m["ticker"].startswith(SERIES + "-")
                            and m["ticker"].rsplit("-", 1)[-1] == coin]
                event("SKIP", reason="selected_coin_quote_not_eligible", coin=coin,
                      market_rows=len(markets), matching_rows=len(matching),
                      quotes=[{"ticker": m.get("ticker"), "status": m.get("status"),
                               "no_ask": m.get("no_ask_dollars"),
                               "close_time": m.get("close_time")}
                              for m in matching[:3]])
                return
            # Refresh the quote before the final funding decision.
            fresh = client.market(plan["ticker"])
            if leader_plan([fresh], coin, start, self.clock()) is None:
                event("SKIP", reason="fresh_quote_changed", coin=coin)
                return
            limit = (D(str(fresh["no_ask_dollars"])) * 100).to_integral_value(
                rounding=ROUND_CEILING) / 100
            quote = _price(fresh["no_ask_dollars"])
            if quote < MIN_NO_ASK or limit > MAX_NO_LIMIT:
                return
            allocation = budget(self.journal)
            available_cash, shards = cash_snapshot(client.balance())
            index = fresh.get("exchange_index")
            if (type(index) is not int or index < 0 or shards is None
                    or index not in shards):
                event("SKIP", reason="exchange_funding_unavailable", coin=coin)
                return
            quantity = _quantity(allocation, limit, min(available_cash, shards[index]))
            if quantity < 1:
                event("SKIP", reason="insufficient_funded_cash", coin=coin)
                return
            if core.entry_market_occupied(client, plan["ticker"]):
                event("SKIP", reason="existing_leader_inventory", coin=coin)
                return
            if not eligible_window(self.clock(), start, ENTRY_SECOND, ENTRY_END_SECOND):
                return
            intent = {"client_id": str(uuid.uuid4()), "created_at": self.clock(),
                      "limit": str(limit)}
            record = {"ticker": plan["ticker"], "coin": coin, "side": "NO",
                      "window_start_ms": start * 1000, "close_at": plan["close"],
                      "quantity": str(quantity), "budget_dollars": str(allocation),
                      "entry_signal_source": "fresh_coinbase_window_returns",
                      "buy": intent, "sells": []}
            self.journal.data["windows"][str(start)] = record
            self.journal.save()
            event("BUY_SUBMIT", ticker=plan["ticker"], coin=coin,
                  side="NO", quantity=quantity, limit=str(limit),
                  budget=str(allocation))
            response = client.place_entry(plan["ticker"], "NO", quantity, limit,
                                          plan["close"], submit_before=start + ENTRY_END_SECOND,
                                          client_order_id=intent["client_id"], ioc=True)
            row = response.get("order", response)
            if row.get("order_id"):
                intent["order_id"] = row["order_id"]
                self.journal.save()
                event("BUY_ACK", ticker=plan["ticker"], order_id=intent["order_id"])
            # Do not retry uncertain or acknowledged submissions in this window.
        except Exception as error:
            event("ENTRY_WAIT", **ranked.error_fields(error))
