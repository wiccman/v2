"""Opt-in, small-inventory market making; no orders are sent on import.

Prices follow the external book midpoint, not the Strike Ruler prediction.
The persisted order ledger is deliberately conservative: buys are costed at
their limit and sales credited at their limit; actual exchange fees are deducted.
"""
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from kalshi import KalshiAPIError

D = Decimal
ZERO = D("0")
ONE = D("1")
TERMINAL = {"canceled", "executed", "rejected"}
ENTRY_WINDOW_SECONDS = 7 * 60
ENTRY_INTERVAL_SECONDS = 60


def entry_minute(now, close_ts):
    elapsed = now - (close_ts - 900)
    return int(elapsed // ENTRY_INTERVAL_SECONDS) if 0 <= elapsed < ENTRY_WINDOW_SECONDS else None


@dataclass(frozen=True)
class MMConfig:
    budget: Decimal = D("10")
    quantity: Decimal = ONE
    levels: int = 5
    level_step: Decimal = D("0.01")
    spread: Decimal = D("0.08")
    fee_reserve: Decimal = D("0.02")  # per contract, per fill; verify market fees
    ttl: int = 15
    poll: int = 3
    max_data_age: int = 5
    stop_before_close: int = 60
    max_mid_move: Decimal = D("0.10")
    cooldown: int = 30

    def __post_init__(self):
        decimals = (self.budget, self.quantity, self.level_step, self.spread, self.fee_reserve, self.max_mid_move)
        if not all(v.is_finite() for v in decimals):
            raise ValueError("MM settings must be finite")
        if not (ZERO < self.budget <= 10 and ZERO < self.quantity <= ONE):
            raise ValueError("MM beta supports at most $10 and one contract at each price")
        if not (1 <= self.levels <= 5 and D('0.0001') <= self.level_step < ONE):
            raise ValueError("MM supports one to five distinct price levels per side")
        if self.quantity != self.quantity.quantize(D('0.01')):
            raise ValueError("MM quantity must use at most two decimal places")
        if not (ZERO <= self.fee_reserve < self.spread / 2 < D('0.5')):
            raise ValueError("MM spread must exceed the two-fill fee reserve")
        if not (1 <= self.poll <= 5 and self.poll + 2 < self.ttl <= 30):
            raise ValueError("MM polling must be 1-5 seconds and TTL poll+3 to 30 seconds")
        if not (1 <= self.max_data_age <= 10 and 30 <= self.stop_before_close < 900):
            raise ValueError("Invalid MM data-age or closing cutoff")
        if not (ZERO < self.max_mid_move < ONE and self.cooldown >= self.poll):
            raise ValueError("Invalid MM volatility pause")

    @classmethod
    def from_env(cls):
        return cls(
            budget=D(os.getenv("MM_BUDGET_DOLLARS", "10")),
            quantity=D(os.getenv("MM_QUOTE_CONTRACTS", "1")),
            levels=int(os.getenv("MM_LEVELS_PER_SIDE", "5")),
            level_step=D(os.getenv("MM_LEVEL_STEP_CENTS", "1")) / 100,
            spread=D(os.getenv("MM_SPREAD_CENTS", "8")) / 100,
            fee_reserve=D(os.getenv("MM_FEE_RESERVE_CENTS", "2")) / 100,
            ttl=int(os.getenv("MM_QUOTE_TTL_SECONDS", "15")),
            poll=int(os.getenv("MM_POLL_SECONDS", "3")),
            max_data_age=int(os.getenv("MM_MAX_DATA_AGE_SECONDS", "5")),
            stop_before_close=int(os.getenv("MM_STOP_BEFORE_CLOSE_SECONDS", "60")),
            max_mid_move=D(os.getenv("MM_MAX_MID_MOVE_CENTS", "10")) / 100,
            cooldown=int(os.getenv("MM_COOLDOWN_SECONDS", "30")),
        )


def snap(price, ranges, up):
    candidates = []
    for item in ranges or [{"start": "0.01", "end": "0.99", "step": "0.01"}]:
        start, end, step = (D(str(item[k])) for k in ("start", "end", "step"))
        if not all(v.is_finite() for v in (start, end, step)) or not (ZERO <= start <= end <= ONE and step > 0):
            raise ValueError("Invalid market price ranges")
        ticks = ((price - start) / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        top = ((end - start) / step).to_integral_value(rounding=ROUND_FLOOR)
        ticks = max(ZERO, ticks) if up else min(top, ticks)
        candidate = start + ticks * step
        if start <= candidate <= end and ZERO < candidate < ONE:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("No valid price tick")
    return min(candidates) if up else max(candidates)


def quote_prices(book, own_orders, market, config, held=ZERO):
    """Remove our resting size so the midpoint cannot be driven by our quotes."""
    levels = {}
    for key in ("yes_dollars", "no_dollars"):
        levels[key] = {}
        for price, size in book[key]:
            price, size = D(str(price)), D(str(size))
            if not price.is_finite() or not size.is_finite() or not (ZERO < price < ONE and size >= ZERO):
                raise ValueError("Invalid book level")
            levels[key][price] = levels[key].get(price, ZERO) + size
    for order in own_orders:
        if order.get("status") != "resting":
            continue
        price = D(order["price"])
        key = "yes_dollars" if order["side"] == "bid" else "no_dollars"
        if key == "no_dollars":
            price = ONE - price
        levels[key][price] = levels[key].get(price, ZERO) - D(order["remaining"])
    yes = [p for p, size in levels["yes_dollars"].items() if size > 0]
    no = [p for p, size in levels["no_dollars"].items() if size > 0]
    if not yes or not no:
        raise ValueError("MM needs external liquidity on both sides")
    best_bid, best_ask = max(yes), ONE - max(no)
    if best_bid >= best_ask:
        raise ValueError("Crossed or locked orderbook")
    mid = (best_bid + best_ask) / 2
    if held:
        # Inventory exits may join the outside ask/bid even when an eight-cent
        # entry spread is impossible near 0/1. Saved targets gate IOC exits.
        return snap(best_bid, market.get("price_ranges"), False), snap(best_ask, market.get("price_ranges"), True), mid
    bid = snap(mid - config.spread / 2, market.get("price_ranges"), False)
    ask = snap(mid + config.spread / 2, market.get("price_ranges"), True)
    if not (bid < best_ask and ask > best_bid and ask - bid >= config.spread):
        raise ValueError("No valid post-only spread")
    return bid, ask, mid


def ladder(bid, ask, market, config, held=ZERO):
    if held:
        side, price, up = ("ask", ask, True) if held > 0 else ("bid", bid, False)
        remaining, quotes = abs(held), []
        while remaining > 0:
            quantity = min(config.quantity, remaining)
            quotes.append((side, price, quantity, True))
            remaining -= quantity
            if remaining:
                try:
                    price = snap(price + (config.level_step if up else -config.level_step), market.get("price_ranges"), up)
                except ValueError:
                    # At the price boundary consolidate the remaining exit size
                    # at the last valid tick; reduce-only caps it to inventory.
                    quotes[-1] = (side, quotes[-1][1], quantity + remaining, True)
                    break
        return quotes
    bids, asks = [bid], [ask]
    for _ in range(1, config.levels):
        bids.append(snap(bids[-1] - config.level_step, market.get("price_ranges"), False))
        asks.append(snap(asks[-1] + config.level_step, market.get("price_ranges"), True))
    if len(set(bids)) != config.levels or len(set(asks)) != config.levels:
        raise ValueError("Insufficient distinct ticks for the full MM ladder")
    return [(side, price, config.quantity, False)
            for side, prices in (("bid", bids), ("ask", asks)) for price in prices]


def ledger(record):
    held, cash = ZERO, ZERO
    for order in record["orders"]:
        count = D(order.get("filled", "0"))
        signed = count if order["side"] == "bid" else -count
        held += signed
        cash -= signed * D(order["price"]) + D(order.get("fees", "0"))
    if record.get("settled"):
        cash += held * D(record["settlement"])
        held = ZERO
    return held, cash


class MarketMaker:
    def __init__(self, client, save, log, config=None, clock=time.time):
        self.client, self.save, self.log = client, save, log
        self.config = config or MMConfig.from_env()
        self.clock = clock

    def _held(self, ticker):
        return sum((D(str(p["position_fp"])) for p in self.client.positions(ticker)
                    if p.get("ticker") == ticker), ZERO)

    def _update(self, order, remote):
        status = remote["status"]
        filled, remaining = D(remote["fill_count_fp"]), D(remote["remaining_count_fp"])
        fees = D(remote["maker_fees_dollars"]) + D(remote["taker_fees_dollars"])
        quantity = D(order["quantity"])
        if status not in TERMINAL | {"resting"} or not all(v.is_finite() for v in (filled, remaining, fees)):
            raise ValueError("Invalid MM order status")
        if not (D(order.get("filled", "0")) <= filled <= quantity and ZERO <= remaining <= quantity - filled):
            raise ValueError("Inconsistent MM fill counts")
        if filled != D(order.get("filled", "0")):
            self.log("MM_FILL", order["ticker"], quantity=str(filled),
                     details=f"{order['client_id']} cumulative fill; fees={fees}")
        order.update(order_id=remote["order_id"], status=status, filled=str(filled),
                     remaining=str(remaining), fees=str(fees))

    def _sync(self, record, state):
        for order in record["orders"]:
            if order.get("status") in TERMINAL:
                continue
            if not order.get("order_id"):
                # Ambiguous POST response: recover the persisted intent, never resubmit it.
                matches = [o for o in self.client.all_orders(order["ticker"])
                           if o.get("client_order_id") == order["client_id"]]
                if len(matches) != 1:
                    raise RuntimeError("MM submission unresolved; new quoting is blocked")
                self._update(order, matches[0])
            else:
                self._update(order, self.client.order(order["order_id"]))
            self.save(state)

    def _cancel(self, record, state):
        """A cancel response alone is insufficient: wait for terminal order status."""
        confirmed = True
        for order in record["orders"]:
            if order.get("status") in TERMINAL:
                continue
            if not order.get("order_id"):
                confirmed = False
                continue
            try:
                self.client.cancel(order["order_id"])
            except KalshiAPIError as error:
                if error.status_code not in (404, 409):
                    raise
            self._update(order, self.client.order(order["order_id"]))
            confirmed &= order["status"] in TERMINAL
            self.save(state)
        return confirmed

    def cancel_all(self, state):
        for record in state.get("mm", {}).get("markets", {}).values():
            if not record.get("settled"):
                self._cancel(record, state)

    def _place(self, record, state, ticker, desired, expiry):
        intents = [dict(ticker=ticker, side=side, price=str(price), quantity=str(quantity),
                        reduce_only=reduce_only, expiry=expiry, client_id=str(uuid.uuid4()),
                        status="pending", filled="0", remaining=str(quantity), fees="0")
                   for side, price, quantity, reduce_only in desired]
        record["orders"].extend(intents)
        self.save(state)  # write-ahead intents for the entire batch
        try:
            results = self.client.place_mm_batch(intents)
        except KalshiAPIError as error:
            if error.status_code in (400, 401, 403, 404, 422, 429):
                for order in intents:
                    order.update(status="rejected", remaining="0")
                self.save(state)
            raise  # timeouts/5xx/409 remain unresolved until recovered
        by_client_id = {o["client_id"]: o for o in intents}
        incomplete = False
        for result in results:
            client_id = result.get("client_order_id")
            if not client_id and result.get("order_id"):
                client_id = self.client.order(result["order_id"])["client_order_id"]
            order = by_client_id.get(client_id)
            if order is None:
                incomplete = True
                continue
            if result.get("error"):
                order.update(status="rejected", remaining="0")
                incomplete = True
            elif result.get("order_id"):
                order["order_id"] = result["order_id"]
                self.log("MM_EXIT_IOC" if order["reduce_only"] else "MM_QUOTE", ticker,
                         price=order["price"], quantity=order["quantity"],
                         details=f"{order['side']}; reduce_only={order['reduce_only']}; expires={expiry}")
            self.save(state)
        self.save(state)
        if incomplete or any(not o.get("order_id") for o in intents):
            raise RuntimeError("MM batch incomplete; canceling accepted quotes before retry/reconciliation")

    def cycle(self, state, market, closed):
        records = state.setdefault("mm", {}).setdefault("markets", {})
        ticker = market["ticker"] if market else None
        # Do not roll unresolved inventory or order state into another contract.
        for old_ticker, old in records.items():
            if old_ticker == ticker or old.get("settled"):
                continue
            self._sync(old, state)
            if not self._cancel(old, state):
                return
            status = self.client.market(old_ticker)
            # Kalshi market responses use finalized; retain settled compatibility.
            if status.get("status") in ("finalized", "settled") and status.get("result") in ("yes", "no"):
                old.update(settled=True, settlement="1" if status["result"] == "yes" else "0")
                self.save(state)
            elif ledger(old)[0] != 0:
                self.log("MM_WAIT_SETTLEMENT", old_ticker)
                return
        if market is None:
            return
        if ticker not in records:
            # A mode change never adopts a manual/legacy position or its exit orders.
            if state.get("markets", {}).get(ticker) or self._held(ticker) != 0 or self.client.all_orders(ticker, "resting"):
                self.log("MM_FOREIGN_INVENTORY", ticker, details="Wait for a clean market before switching modes")
                return
            records[ticker] = {"orders": []}
            self.save(state)
        record = records[ticker]
        self._sync(record, state)
        own_ids = {o.get("order_id") for o in record["orders"]}
        if any(o["order_id"] not in own_ids for o in self.client.all_orders(ticker, "resting")):
            self._cancel(record, state)
            self.log("MM_FOREIGN_ORDERS", ticker)
            return
        held, _ = ledger(record)
        if self._held(ticker) != held or abs(held) > self.config.quantity * self.config.levels:
            self._cancel(record, state)
            self.log("MM_POSITION_MISMATCH", ticker)
            return
        now = self.clock()
        close_ts = closed.timestamp()
        if now >= close_ts:
            self._cancel(record, state)
            return
        if not held and now < record.get("pause_until", 0):
            self._cancel(record, state)
            return
        balance = self.client.balance()
        available = D(str(balance["balance_dollars"])) if "balance_dollars" in balance else D(str(balance["balance"])) / 100
        data_at = self.clock()
        current = self.client.market(ticker)
        if current.get("status") not in ("open", "active"):
            self._cancel(record, state)
            return
        try:
            book = self.client.orderbook(ticker)
            bid, ask, mid = quote_prices(book, record["orders"], current, self.config, held)
        except (ValueError, KeyError):
            self._cancel(record, state)
            self.log("MM_INVALID_BOOK", ticker)
            return
        now = self.clock()
        if now - data_at > self.config.max_data_age:
            self._cancel(record, state)
            self.log("MM_STALE_DATA", ticker)
            return
        previous_mid = D(record.get("mid", str(mid)))
        record["mid"] = str(mid)
        if not held and abs(mid - previous_mid) >= self.config.max_mid_move:
            record["pause_until"] = now + self.config.cooldown
            self.save(state)
            self._cancel(record, state)
            self.log("MM_VOLATILITY_PAUSE", ticker)
            return
        minute = entry_minute(now, close_ts)
        # Fix the first exit ladder's anchor instead of chasing an ask/bid that
        # an immediate-or-cancel order can never reach. Never remove reduce-only.
        if held:
            exit_side = "ask" if held > 0 else "bid"
            if record.get("exit_side") != exit_side or "exit_anchor" not in record:
                record["exit_side"] = exit_side
                record["exit_anchor"] = str(ask if held > 0 else bid)
                self.save(state)
                self.log("MM_EXIT_TARGET", ticker, price=record["exit_anchor"],
                         quantity=str(abs(held)), details=exit_side)
            anchor = D(record["exit_anchor"])
            exit_bid, exit_ask = (bid, anchor) if held > 0 else (anchor, ask)
        else:
            record.pop("exit_side", None)
            record.pop("exit_anchor", None)
        try:
            if held:
                desired = ladder(exit_bid, exit_ask, current, self.config, held)
                desired = [q for q in desired if (bid >= q[1] if held > 0 else ask <= q[1])]
            else:
                desired = ladder(bid, ask, current, self.config) if minute is not None else []
        except ValueError:
            self._cancel(record, state)
            self.log("MM_LADDER_UNAVAILABLE", ticker)
            return
        live = [o for o in record["orders"] if o.get("status") not in TERMINAL]
        specs = [(o["side"], D(o["price"]), D(o["remaining"]), o["reduce_only"]) for o in live]
        if specs == desired and all(o["expiry"] > now + self.config.poll + 2 for o in live):
            self.save(state)
            return
        if live:
            self._cancel(record, state)
            return  # fresh order/position/book snapshot on the next poll, including cancel-race fills
        if not desired:
            return
        if held == 0:
            if minute <= record.get("last_entry_minute", -1):
                return
            # Ledger includes past realized losses/fees; profits do not increase order size.
            capital = self.config.budget + sum((ledger(r)[1] + min(ledger(r)[0], ZERO) for r in records.values()), ZERO)
            required = sum((qty * ((price if side == "bid" else ONE - price) + self.config.fee_reserve)
                            for side, price, qty, _ in desired), ZERO)
            if not available.is_finite() or min(capital, available, self.config.budget) < required:
                self.log("MM_BUDGET_BLOCK", ticker, details=f"required={required}; capital={capital}; cash={available}")
                return
        entry_cutoff = close_ts - 900 + ENTRY_WINDOW_SECONDS
        expiry = int(min(now + self.config.ttl, close_ts if held else entry_cutoff))
        if expiry <= now + self.config.poll:
            return
        try:
            if self.clock() - data_at > self.config.max_data_age or self.clock() >= expiry - 1:
                raise RuntimeError("MM quote snapshot expired before submission")
            if not held:
                submit_minute = entry_minute(self.clock(), close_ts)
                if submit_minute is None or submit_minute <= record.get("last_entry_minute", -1):
                    return
                # Durable slot reservation prevents duplicate batches on restart
                # or an ambiguous response. Missed minutes are never burst-replayed.
                record["last_entry_minute"] = submit_minute
                self.save(state)
                self.log("MM_ENTRY_MINUTE", ticker, details=str(submit_minute))
            self._place(record, state, ticker, desired, expiry)
        except Exception:
            record["pause_until"] = self.clock() + self.config.cooldown
            self.save(state)
            self._cancel(record, state)
            raise
