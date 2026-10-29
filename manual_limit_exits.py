"""Fill-attributed manual targets, executed as price-limited reduce-only IOCs.

Kalshi V2 rejects reduce_only with GTC. This worker therefore keeps its target
locally and submits an IOC only when the bid reaches it. It never opens a buy,
leaves an unprotected resting sell, or changes the automatic strategy's budget.
The parent exit monitor calls this worker under the shared execution lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from co_trading import MANUAL_GROSS_PROFIT_GOAL
from price_pairs import InventorySyncError, _remaining_lots, _outcome_cost
from take_profit import TakeProfitMonitor

SCHEMA = 3
ACTIVE_ORDER_STATES = {"resting", "pending"}
TERMINAL_ORDER_STATES = {"executed", "canceled", "cancelled", "expired", "rejected"}


class TargetUnreachable(ValueError):
    """The selected profit target has no valid exchange price."""


class ManualExitBlocked(InventorySyncError):
    """An external snapshot has not proved that these contracts can be sold."""


def failure_context(error: Exception) -> dict:
    """Report the failed check without logging response bodies or credentials."""
    result = {"error_type": type(error).__name__}
    reasons = {
        "Order direction is missing or contradictory",
        "Resting exit identity or terms changed",
        "Resting exit counts/status are inconsistent",
        "Resting reservation changed during reconciliation",
        "An existing resting order can reduce the shared position",
        "Fills and position disagree; waiting for consistent exchange data",
        "Exit order/fill history has not converged",
        "Managed entry order/fill counts have not converged",
        "External order/fill counts have not converged",
        "Previously adopted fill identity/cost changed",
        "Previously adopted opening disappeared from fill history",
        "Manual/automatic ownership snapshot is stale",
        "Ownership changed before submission; reconcile again",
        "Resting exits exceed verified owned inventory",
        "Cancellation ownership could not be verified",
        "Exit fill exceeds its attributable inventory",
        "Fill ordering is ambiguous at a shared timestamp",
    }
    if str(error) in reasons:
        result["reason"] = str(error)
    status = getattr(error, "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    trace = error.__traceback__
    while trace:
        source = Path(trace.tb_frame.f_code.co_filename).name
        if source in {"manual_limit_exits.py", "resting_manual_exits.py",
                      "manual_exit_runner.py", "price_pairs.py", "kalshi.py"}:
            result.update(check_source=source, check_line=trace.tb_lineno)
        trace = trace.tb_next
    return result


def number(value: Any, name: str) -> D:
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ManualExitBlocked(f"Invalid {name}") from error
    if not result.is_finite():
        raise ManualExitBlocked(f"Non-finite {name}")
    return result


def quantity(value: Any) -> D:
    result = number(value, "contract quantity")
    if result < 0 or result.quantize(D("0.01")) != result:
        raise ManualExitBlocked("Contract quantity must be nonnegative with at most two decimals")
    return result


def timestamp(row: dict) -> D:
    if row.get("created_time"):
        value = datetime.fromisoformat(row["created_time"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ManualExitBlocked("Fill timestamp requires a timezone")
        return number(value.timestamp(), "fill timestamp")
    return number(row.get("ts"), "fill timestamp")


def direction(row: dict) -> int:
    """Canonical book direction; deprecated action/side are fallback only.

    Kalshi's direction migration specifies book_side/outcome_side as the
    authoritative pair. A stale deprecated representation must not invalidate
    a correctly identified exit. Conflicting canonical fields still block.
    """
    sign = {"bid": 1, "ask": -1}.get(str(row.get("book_side", "")).lower())
    outcome = {"yes": 1, "no": -1}.get(str(row.get("outcome_side", "")).lower())
    canonical = [n for n in (sign, outcome) if n is not None]
    invalid = ((row.get("book_side") is not None and sign is None)
               or (row.get("outcome_side") is not None and outcome is None))
    if invalid or len(set(canonical)) > 1:
        raise ManualExitBlocked("Order direction is missing or contradictory")
    if canonical:
        return canonical[0]
    action, side = str(row.get("action", "")).lower(), str(row.get("side", "")).lower()
    if action in {"buy", "sell"} and side in {"yes", "no"}:
        return (1 if side == "yes" else -1) * (1 if action == "buy" else -1)
    raise ManualExitBlocked("Order direction is missing or contradictory")


def known_ids(record: Any) -> tuple[set[str], set[str]]:
    """Read bot-owned IDs, including legacy routes, without adopting their fills."""
    orders, clients = set(), set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and item:
                    if key == "order_id" or key.endswith("_order_id"):
                        orders.add(item)
                    if key in {"client_id", "client_order_id"}:
                        clients.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(record)
    return orders, clients


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def record_digest(record: dict) -> str:
    orders, clients = known_ids(record)
    return digest([record.get("entry_intents", []), sorted(orders), sorted(clients)])


def rows_digest(rows: list[dict]) -> str:
    # Endpoint order is not significant. A conflicting duplicate is rejected by
    # the replay before this fingerprint is accepted.
    return digest(sorted(set(json.dumps(r, sort_keys=True) for r in rows)))


def primary_orders(rows: list[dict], ticker: str) -> list[dict]:
    by_id = {}
    for row in rows:
        if row.get("subaccount_number", 0) != 0:
            continue
        if row.get("ticker") != ticker or not row.get("order_id"):
            raise ManualExitBlocked("Order identity/ticker is missing or mismatched")
        oid = row["order_id"]
        if oid in by_id and by_id[oid] != row:
            raise ManualExitBlocked("Conflicting duplicate order")
        by_id[oid] = row
    return list(by_id.values())


def position(rows: list[dict], ticker: str) -> D:
    matches = [p for p in rows if p.get("ticker") == ticker and p.get("subaccount_number", 0) == 0]
    if len(matches) > 1:
        raise ManualExitBlocked("Multiple primary-account position rows")
    held = number(matches[0].get("position_fp"), "position") if matches else D(0)
    quantity(abs(held))
    return held


def round_target(raw: D, ranges: list[dict] | None, sign: int) -> D:
    """Smallest outcome sell price meeting the goal on the market's YES grid."""
    if not raw.is_finite() or sign not in {-1, 1}:
        raise ManualExitBlocked("Invalid target")
    # Whole cents are valid on current grids; never infer a sub-cent tick from
    # the human-readable price_level_structure name.
    bands = ranges if ranges is not None else [{"start": "0", "end": "1", "step": ".01"}]
    candidates = []
    if not bands:
        raise ManualExitBlocked("Empty market price grid")
    for band in bands:
        start, end, step = (number(band.get(k), "price grid") for k in ("start", "end", "step"))
        if (not 0 <= start < end <= 1 or not D("0.0001") <= step <= 1
                or any(x.quantize(D("0.0001")) != x for x in (start, end, step))):
            raise ManualExitBlocked("Invalid market price grid")
        if sign > 0:
            wire = start + ((max(raw, start) - start) / step).to_integral_value(rounding=ROUND_CEILING) * step
        else:
            wire = start + ((min(1 - raw, end) - start) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        outcome = wire if sign > 0 else 1 - wire
        if start <= wire <= end and 0 < wire < 1 and outcome >= raw:
            candidates.append(outcome)
    if not candidates:
        raise TargetUnreachable("Gross profit goal is not reachable on the market's tradable grid")
    return min(candidates)


class _DurableTransport(TakeProfitMonitor):
    """Reuse the tested IOC journal/reconciliation without starting another thread."""
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class ManualLimitExitWorker:
    """Complete opt-in manual exit lifecycle, separate from automatic holdings.

    With this feature enabled, new non-bot primary-account limit-order exposure
    in the allowed series is treated as manual. Kalshi does not supply a human-
    versus-API origin flag here. Another API writer therefore requires a separate
    account/subaccount or must not share the managed series.
    """
    def __init__(self, client, path, *, profit_goal=MANUAL_GROSS_PROFIT_GOAL,
                 clock: Callable[[], float] = time.time, emit=None,
                 series=("KXBTC15M",), discovery_seconds=1.0, dry_run=False,
                 price_increment=None):
        self.client, self.clock = client, clock
        self.dry_run = bool(dry_run)
        self.path = Path(path)
        self.profit_goal = number(profit_goal, "manual gross profit goal")
        if self.profit_goal <= 0:
            raise ValueError("Manual gross profit goal must be positive")
        self.price_increment = (number(price_increment, "manual price increment")
                                if price_increment is not None else None)
        if self.price_increment is not None and not D(0) < self.price_increment < D(1):
            raise ValueError("Manual price increment must be between zero and one dollar")
        self.target_policy = ({"mode": "price_increment", "increment": str(self.price_increment.normalize())}
                              if self.price_increment is not None else {"mode": "order_gross_profit"})
        self.series = tuple(series)
        if (not self.series
                or any(not isinstance(s, str) or not s or not s.isalnum() for s in self.series)
                or len(self.series) != len(set(self.series))):
            raise ValueError("Explicit alphanumeric manual series are required")
        self.discovery_seconds = float(max(D(1), number(discovery_seconds, "discovery interval")))
        self.emit = emit or (lambda event, **data: print(json.dumps({"event": event, **data}), flush=True))
        existed = self.path.exists()
        self.transport = _DurableTransport(client, lambda: {}, path, clock=clock,
            poll=1.0, no_fill_pause=3.0,
            emit=lambda event, **data: self.emit("MANUAL_" + event, **data))
        self.state = self.transport.state
        self._validate_execution_mode()
        key_scope = digest(str(getattr(client, "key_id", "")))
        if existed:
            if self.state.get("schema") not in {2, SCHEMA} or self.state.get("key_scope") != key_scope:
                raise ManualExitBlocked("Manual journal schema/account changed; review before enabling")
            previous = self.state.get("series")
            if (not isinstance(previous, list) or not previous
                    or any(not isinstance(s, str) or not s.isalnum() for s in previous)
                    or len(previous) != len(set(previous))
                    or tuple(previous) != self.series[:len(previous)]):
                raise ManualExitBlocked("Manual series scope changed; review the existing journal")
            if self.state.get("target_policy", {"mode": "order_gross_profit"}) != self.target_policy:
                raise ManualExitBlocked("Manual target policy changed; do not reuse an incompatible journal")
            activation = number(self.state.get("activation_time"), "activation time")
            if activation < 0:
                raise ManualExitBlocked("Invalid activation time")
            # Old journals had one activation time. Preserve that cutoff for
            # their existing series; newly enabled series start at this launch.
            # Schema 3 makes older runners fail closed instead of ignoring the
            # new per-series cutoffs when an operator rolls back the code.
            if self.state["schema"] == 2:
                if "series_activation_times" in self.state:
                    raise ManualExitBlocked("Legacy journal has unexpected series activation times")
                activations = {s: str(activation) for s in previous}
            else:
                activations = self.state.get("series_activation_times")
                if not isinstance(activations, dict) or set(activations) != set(previous):
                    raise ManualExitBlocked("Manual series activation times are incomplete")
                if any(number(t, "series activation time") < activation for t in activations.values()):
                    raise ManualExitBlocked("Manual series activation precedes original activation")
                activations = dict(activations)
            added = self.series[len(previous):]
            if added:
                enabled_at = number(clock(), "series activation time")
                if enabled_at < max(number(t, "series activation time") for t in activations.values()):
                    raise ManualExitBlocked("Clock precedes existing series activation")
                activations.update({s: str(enabled_at) for s in added})
            if self.state["schema"] != SCHEMA or added:
                self.state.update(schema=SCHEMA, series=list(self.series),
                                  series_activation_times=activations)
                self.save()
                if added:
                    self.emit("MANUAL_SERIES_ADDED", series=list(added),
                              activation_time=str(enabled_at))
        else:
            activation = number(clock(), "activation time")
            if activation < 0:
                raise ManualExitBlocked("Invalid activation time")
            self.state.update(schema=SCHEMA, activation_time=str(activation),
                              key_scope=key_scope, series=list(self.series), target_policy=self.target_policy,
                              series_activation_times={s: str(activation) for s in self.series})
            self.save()
        self.read_entries = lambda: {"markets": {}}
        self._snapshots: dict[str, dict] = {}
        self._plans: dict[str, list[dict]] = {}
        self._last_discovery = None
        self._discovered: set[str] = set()

    def _validate_execution_mode(self):
        """Subclass startup checks must finish before a journal upgrade is saved."""

    def bind(self, read_entries):
        self.read_entries = read_entries

    def save(self):
        self.transport.save()

    def ledger(self, ticker: str) -> dict:
        return self.state["markets"].setdefault(ticker, {})

    def allowed(self, ticker: str) -> bool:
        return isinstance(ticker, str) and any(ticker.startswith(s + "-") for s in self.series)

    def activation_time(self, ticker: str) -> D:
        return number(self.state["series_activation_times"][ticker.split("-", 1)[0]],
                      "series activation time")

    def discover(self) -> set[str]:
        now = self.clock()
        if self._last_discovery is None or now - self._last_discovery >= self.discovery_seconds:
            found, cursors = set(), set()
            params = {"limit": 1000, "subaccount": 0}
            while True:
                page = self.client.request("GET", "/portfolio/positions", params=dict(params), auth=True)
                for row in page["market_positions"]:
                    if row.get("subaccount_number", 0) != 0:
                        continue
                    ticker = row.get("ticker")
                    if self.allowed(ticker) and number(row.get("position_fp"), "position"):
                        found.add(ticker)
                cursor = page.get("cursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise ManualExitBlocked("Position pagination cursor repeated/invalid")
                cursors.add(cursor)
                params["cursor"] = cursor
            self._discovered, self._last_discovery = found, now
        return self._discovered | set(self.state["markets"])

    def pending(self, ticker: str) -> bool:
        return bool(self.state["markets"].get(ticker, {}).get("pending"))

    def reconcile(self, ticker: str) -> bool:
        # Also reconcile old managed markets even after a later scope change is
        # rejected at startup. There is never a silent journal reset.
        ledger = self.ledger(ticker)
        if self.clock() < ledger.get("retry_after", 0):
            return False
        return self.transport._reconcile(ticker, ledger)

    def combined_exits(self, ticker: str, automatic: dict) -> dict:
        manual = self.state["markets"].get(ticker, {}).get("exit_orders", {})
        if set(automatic) & set(manual):
            raise ManualExitBlocked("An exit order cannot belong to both strategies")
        return {**automatic, **manual}

    def owns_resting_order(self, ticker: str, row: dict) -> bool:
        # IOC workers never own a standing reservation. The resting worker
        # overrides this only for orders recovered from its durable journal.
        return False

    def observe(self, ticker, record, automatic_ledger, fills, entry_orders, exit_orders, held):
        started = self.clock()
        if self.pending(ticker) or automatic_ledger.get("pending"):
            raise ManualExitBlocked("Unresolved exit acknowledgement")
        recovered = automatic_ledger.get("entry_order_ids", {})
        if any(not i.get("order_id") and not i.get("entry_closed") and
               not recovered.get(i.get("client_id")) for i in record.get("entry_intents", [])):
            raise ManualExitBlocked("Unresolved automatic entry acknowledgement")
        orders = primary_orders(self.client.all_orders(ticker), ticker)
        by_order = {o["order_id"]: o for o in orders}
        bot_ids, bot_clients = known_ids(record)
        bot_ids |= set(entry_orders) | set(recovered.values()) | set(automatic_ledger.get("exit_orders", {}))
        for row in orders:
            if row.get("client_order_id") in bot_clients:
                bot_ids.add(row["order_id"])
            status = str(row.get("status", "")).lower()
            if status in ACTIVE_ORDER_STATES:
                count = quantity(row.get("remaining_count_fp", row.get("remaining_count")))
                if count and held and direction(row) * held < 0 and not self.owns_resting_order(ticker, row):
                    # A user sell or opposing resting buy could consume these
                    # contracts. Do not cancel it or submit a competing exit.
                    raise ManualExitBlocked("An existing resting order can reduce the shared position")
            elif status not in TERMINAL_ORDER_STATES:
                raise ManualExitBlocked("Unknown order status")
        openings, reductions, outside = [], [], []
        lots = _remaining_lots(fills, entry_orders, exit_orders, held, ticker, outside,
                              include_untracked=True, openings=openings, reductions=reductions)
        unique = {f.get("fill_id") or f.get("trade_id"): f for f in fills}
        observed_counts = defaultdict(lambda: D(0))
        for row in unique.values():
            if not row.get("order_id"):
                raise ManualExitBlocked("Fill order identity unavailable")
            observed_counts[row["order_id"]] += quantity(row.get("count_fp", row.get("count")))
        ledger = self.ledger(ticker)
        managed = ledger.setdefault("managed_fills", {})
        goals = ledger.setdefault("order_goals", {})
        changed = False
        for lot in openings:
            fid, row = lot["fill_id"], lot["fill"]
            oid, stamp = row["order_id"], timestamp(row)
            if fid in managed:
                if lot["entry"] is not None or oid in bot_ids:
                    raise ManualExitBlocked("Manual fill conflicts with automatic ownership")
                saved = managed[fid]
                if (saved["order_id"] != oid or saved["sign"] != lot["sign"] or
                    number(saved["cost"], "saved fill cost") != _outcome_cost(row, lot["sign"]) or
                    number(saved["timestamp"], "saved timestamp") != stamp or
                    number(saved["opened_quantity"], "saved opening") != lot["quantity"]):
                    raise ManualExitBlocked("Previously adopted fill identity/cost changed")
                continue
            if (lot["entry"] is not None or oid in bot_ids or not self.allowed(ticker)
                    or stamp < self.activation_time(ticker)):
                continue
            remote = by_order.get(oid)
            if remote is None:
                raise ManualExitBlocked("External fill has no matching order yet")
            if remote.get("type") != "limit" or str(remote.get("action", "")).lower() == "sell":
                continue
            if direction(remote) != lot["sign"]:
                raise ManualExitBlocked("External order and fill directions disagree")
            if quantity(remote.get("fill_count_fp", remote.get("fill_count"))) != observed_counts[oid]:
                raise ManualExitBlocked("External order/fill counts have not converged")
            if stamp > number(self.clock() + 1, "clock"):
                raise ManualExitBlocked("Fill timestamp is ahead of the local clock")
            managed[fid] = {"order_id": oid, "sign": lot["sign"],
                            "cost": str(_outcome_cost(row, lot["sign"])),
                            "timestamp": str(stamp), "opened_quantity": str(lot["quantity"])}
            if self.price_increment is None:
                goals.setdefault(oid, str(self.profit_goal))
            changed = True
            self.emit("MANUAL_FILL_ADOPTED", ticker=ticker, fill_id=fid, order_id=oid,
                      quantity=str(lot["quantity"]), target_policy=self.target_policy,
                      fill_timestamp=str(stamp), detected_timestamp=str(self.clock()),
                      gross_profit_goal=goals.get(oid),
                      raw_target=(str(D(managed[fid]["cost"]) + self.price_increment)
                                  if self.price_increment is not None else None))
        if set(managed) - {lot["fill_id"] for lot in openings}:
            raise ManualExitBlocked("Previously adopted opening disappeared from fill history")
        # Confirm every managed buy's complete counts, including later partial
        # fills; a stale aggregate order response is not safe sizing evidence.
        for oid in {m["order_id"] for m in managed.values()}:
            remote = by_order.get(oid)
            if remote is None or quantity(remote.get("fill_count_fp", remote.get("fill_count"))) != observed_counts[oid]:
                raise ManualExitBlocked("Managed entry order/fill counts have not converged")
        realized = defaultdict(lambda: D(0))
        for reduction in reductions:
            saved = managed.get(reduction["entry_fill_id"])
            if saved:
                sale = unique[reduction["exit_fill_id"]]
                realized[saved["order_id"]] += reduction["quantity"] * (
                    _outcome_cost(sale, saved["sign"]) - D(saved["cost"]))
        remaining = defaultdict(list)
        for lot in lots:
            if lot["fill_id"] in managed:
                if lot["entry"] is not None:
                    raise ManualExitBlocked("Manual fill has become automatic inventory")
                remaining[managed[lot["fill_id"]]["order_id"]].append(lot)
        plans, summaries = [], {}
        if self.price_increment is not None:
            # Each fill keeps its own purchase price plus the increment. A better
            # sale, manual reduction, later fill or restart cannot lower another
            # fill's target or turn its target into a total-dollar profit goal.
            for oid, members in remaining.items():
                summaries[oid] = {"mode": "price_increment", "increment": str(self.price_increment),
                                  "realized_gross": str(realized[oid]), "fills": []}
                for lot in members:
                    fid, qty = lot["fill_id"], lot["quantity"]
                    entry = D(managed[fid]["cost"])
                    raw_target = entry + self.price_increment
                    plans.append({"order_id": oid, "fill_id": fid, "sign": lot["sign"],
                                  "quantity": qty, "cost": entry * qty, "realized": D(0),
                                  "goal": qty * self.price_increment, "raw_target": raw_target,
                                  "allocations": [{"fill_id": fid, "quantity": str(qty)}]})
                    summaries[oid]["fills"].append({"fill_id": fid, "remaining_quantity": str(qty),
                                                   "entry_price": str(entry), "raw_target": str(raw_target)})
        for oid, goal_text in goals.items():
            if self.price_increment is not None:
                raise ManualExitBlocked("Total-profit goals found in an increment journal")
            goal = number(goal_text, "saved manual profit goal")
            if goal <= 0:
                raise ManualExitBlocked("Saved manual profit goal must be positive")
            members = remaining.get(oid, [])
            qty = sum((lot["quantity"] for lot in members), D(0))
            cost = sum((lot["quantity"] * D(managed[lot["fill_id"]]["cost"]) for lot in members), D(0))
            summaries[oid] = {"remaining_quantity": str(qty), "remaining_cost": str(cost),
                              "realized_gross": str(realized[oid]), "gross_profit_goal": str(goal)}
            if qty:
                signs = {lot["sign"] for lot in members}
                if len(signs) != 1:
                    raise ManualExitBlocked("One manual buy order has mixed directions")
                plans.append({"order_id": oid, "sign": signs.pop(), "quantity": qty,
                              "cost": cost, "realized": realized[oid], "goal": goal,
                              "raw_target": (cost + max(D(0), goal - realized[oid])) / qty,
                              "allocations": [{"fill_id": lot["fill_id"], "quantity": str(lot["quantity"])}
                                              for lot in members]})
        if ledger.get("profit_summary") != summaries:
            ledger["profit_summary"] = summaries
            changed = True
        if changed:
            self.save()
        self._plans[ticker] = plans
        self._snapshots[ticker] = {"held": held, "fills": rows_digest(fills),
                                  "orders": rows_digest(orders), "record": record_digest(record),
                                  "checked_at": started}

    def verify_snapshot(self, ticker: str) -> None:
        snapshot = self._snapshots.get(ticker)
        if not snapshot or not 0 <= self.clock() - snapshot["checked_at"] <= 10:
            raise ManualExitBlocked("Manual/automatic ownership snapshot is stale")
        held = position(self.client.positions(ticker), ticker)
        fills = self.client.all_fills(ticker)
        orders = primary_orders(self.client.all_orders(ticker), ticker)
        record = self.read_entries().get("markets", {}).get(ticker, {})
        if (held != snapshot["held"] or rows_digest(fills) != snapshot["fills"] or
                rows_digest(orders) != snapshot["orders"] or record_digest(record) != snapshot["record"]
                or self.clock() - snapshot["checked_at"] > 10):
            raise ManualExitBlocked("Ownership changed before submission; reconcile again")

    def maybe_submit(self, ticker: str, record: dict, automatic_ledger: dict) -> bool:
        ledger = self.ledger(ticker)
        if (ledger.get("window_closed") or self.pending(ticker) or automatic_ledger.get("pending")
                or self.clock() < max(ledger.get("next_attempt_at", 0), ledger.get("retry_after", 0))):
            return False
        plans = self._plans.get(ticker, [])
        if not plans:
            return False
        market = self.client.market(ticker)
        if market.get("status") not in {"active", "open"}:
            return False
        close = record.get("close_timestamp", automatic_ledger.get("close_timestamp"))
        if close is None:
            close_time = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
            if close_time.tzinfo is None:
                raise ManualExitBlocked("Market close requires a timezone")
            close = close_time.timestamp()
        if not number(close, "market close").is_finite() or self.clock() >= float(close):
            self.window_closed(ticker, close)
            return False
        last = ledger.get("last_manual_order", "")
        ordered = sorted(plans, key=lambda p: (p["order_id"] <= last, p["order_id"]))
        for plan in ordered:
            try:
                target = round_target(plan["raw_target"], market.get("price_ranges"), plan["sign"])
            except TargetUnreachable:
                reason = {"order_id": plan["order_id"], "required_price": str(plan["raw_target"])}
                if ledger.get("unreachable") != reason:
                    ledger["unreachable"] = reason
                    self.save()
                    self.emit("MANUAL_TARGET_UNREACHABLE", ticker=ticker, **reason,
                              action="wait_no_lower_profit_fallback")
                continue
            side = "yes" if plan["sign"] > 0 else "no"
            bid = number(market.get(side + "_bid_dollars"), "manual exit bid")
            if not 0 <= bid <= 1:
                raise ManualExitBlocked("Manual exit bid out of bounds")
            if bid < target:
                continue
            self.verify_snapshot(ticker)
            qty = quantity(plan["quantity"])
            if not qty or qty > abs(self._snapshots[ticker]["held"]):
                raise ManualExitBlocked("Manual plan exceeds owned inventory")
            allocation = {"allocations": plan["allocations"], "cost_groups": [{
                "order_id": plan["order_id"], "gross_profit_goal": str(plan["goal"]),
                "realized_gross": str(plan["realized"]), "remaining_cost": str(plan["cost"])}]}
            ledger["last_manual_order"] = plan["order_id"]
            if self.dry_run:
                self.emit("MANUAL_DRY_RUN_TARGET", ticker=ticker, order_id=plan["order_id"],
                          quantity=str(qty), target=str(target), would_submit=True)
                return False
            return self.transport._submit(ticker, ledger, close, plan["sign"] * qty, target,
                paired=False, purpose="manual_take_profit", plan=allocation)
        return False

    def window_closed(self, ticker: str, close: Any) -> None:
        ledger = self.ledger(ticker)
        if not ledger.get("window_closed"):
            ledger["window_closed"] = str(close)
            self.save()
            self.emit("MANUAL_WINDOW_CLOSED", ticker=ticker,
                      pending_ack=self.pending(ticker), action="no_new_orders_not_assumed_sold")
        self._plans.pop(ticker, None)
        self._snapshots.pop(ticker, None)
