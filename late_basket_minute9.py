"""Additional basket-ranked late entry for 15-minute single-coin contracts.

At minutes 12..15, use the same fresh Coinbase 15m ranking as the ordinary
minute-nine policy. Consider only the original top two ranks; exclude BTC
without promoting third place, and honor the existing SOL Central-time veto.

Buy the aligned YES/NO single-coin contract with an 89..97 cent limit, with
a *combined* additional allocation of half the current active trade budget.
Zone 1 and Zone 2 have independent journal keys. An explicitly verified
Zone 1 no-fill or fully closed position does not suppress a Zone 2 opportunity.
No duplicate exposure, no outside inventory adoption, no cash transfers.
Existing entry/exit ownership, verified fills and 21c stop apply normally.
Place a resting 98c take-profit after the exchange confirms an entry fill,
while preserving the existing 21c-below-entry stop. Never buy at or above
the 98c exit target: that would guarantee no gross profit on the sale. Extra late results are
included in realized P&L but do not change the *regular* sizing win streak.
"""
from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
import cash_checked_minute9 as cash

LATE_START_SECONDS = 720
LATE_END_SECONDS = 900
LATE_MIN_PRICE = D(".89")
LATE_MAX_PRICE = D(".97")
LATE_TAKE_PROFIT = D(".98")
MAX_LATE_COINS = 2
LATE_KIND = "late_basket_rank"
LATE_ZONE = core.ZONE2


def zone2_attempt_exists(journal, ticker):
    """Legacy and new Zone 2 records both forbid repeat buys in this zone."""
    legacy = journal.data["windows"].get(ticker)
    return (core.zone_record_key(ticker, LATE_ZONE) in journal.data["windows"]
            or bool(legacy and legacy.get("entry_kind") == LATE_KIND))


def zone2_entry_status(client, journal, ticker, *, expected_client_id=None):
    """Allow an independently tracked zone only when earlier inventory is safe.

    Unknown acceptance or incomplete Zone 1 reconciliation is always a veto.
    Full Zone 1 exits may reenter only after verifying zero held inventory,
    terminal orders, and fills owned by the original saved order identities.
    """
    windows = journal.data["windows"]
    zone2_key = core.zone_record_key(ticker, LATE_ZONE)
    existing = windows.get(zone2_key)
    if existing is not None:
        if (expected_client_id is None
                or existing.get("buy", {}).get("client_id") != expected_client_id):
            return False, "zone2_already_attempted"
    regular = windows.get(core.zone_record_key(ticker, core.ZONE1))
    owned_order_ids = set()
    if regular is not None:
        if regular.get("entry_kind") == LATE_KIND:
            return False, "legacy_zone2_already_attempted"
        if regular.get("ticker") != ticker:
            return False, "zone1_identity_mismatch"
        outcome = regular.get("outcome") or {}
        status = outcome.get("status")
        if status == "no_fill":
            if not cash.entry_slot_released(regular):
                return False, "zone1_zero_fill_unverified"
        elif status in {"win", "loss", "flat"}:
            try:
                bought = D(str(outcome["bought"]))
                sold = D(str(outcome["sold"]))
                remaining = D(str(outcome["remaining_at_settlement"]))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                return False, "zone1_exit_unverified"
            if (not all(q.is_finite() for q in (bought, sold, remaining))
                    or bought <= 0 or sold != bought or remaining != 0):
                return False, "zone1_inventory_not_fully_closed"
        else:
            return False, "zone1_order_unresolved"
        owned_order_ids = {
            x["order_id"] for x in [regular.get("buy", {})] + regular.get("sells", [])
            if x.get("order_id")
        }
    # A final freshness check of inventory and order history makes zone
    # independence safe even if the previous Zone 1 order has completed.
    if core.position_for(client, ticker) != 0:
        return False, "market_inventory_present"
    orders = client.all_orders(ticker)
    if any(order.get("order_id") not in owned_order_ids
           or order.get("status") not in core.TERMINAL for order in orders):
        return False, "other_or_pending_market_order"
    fills = client.all_fills(ticker)
    if any(fill.get("order_id") not in owned_order_ids for fill in fills):
        return False, "outside_market_fills"
    return True, "zone2_independent_entry_allowed"


def in_late_window(now, start):
    return (math.isfinite(now) and type(start) is int and start % 900 == 0
            and LATE_START_SECONDS <= now - start < LATE_END_SECONDS)


def late_plan(market, start, now, series, side):
    if not in_late_window(now, start) or side not in {"YES", "NO"}:
        return None
    if market.get("status") not in {"active", "open"}:
        return None
    ticker = market.get("ticker")
    if not isinstance(ticker, str) or not ticker.startswith(series + "-"):
        return None
    try:
        close = core.parse_time(market["close_time"])
        price = D(str(market[side.lower() + "_ask_dollars"]))
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return None
    if close != start + 900 or not price.is_finite():
        return None
    if not LATE_MIN_PRICE <= price <= LATE_MAX_PRICE:
        return None
    limit = (price * 100).to_integral_value(rounding=ROUND_CEILING) / 100
    if limit > LATE_MAX_PRICE:
        return None
    return {"ticker": ticker, "series": series, "side": side,
            "limit": str(limit), "ask": str(price),
            "window_start_ms": start * 1000, "window_end_ms": int(close * 1000)}


def half_budget_reservation(journal, start):
    # Reserve ALL submitted or possibly submitted late entries including a
    # zero fill until reconciliation. Conservative across process restarts.
    total = D(0)
    for r in journal.data["windows"].values():
        if (r.get("entry_kind") != LATE_KIND
                or r.get("window_start_ms") != start * 1000
                or not isinstance(r.get("buy"), dict)):
            continue
        total += D(str(r["quantity"])) * (D(str(r["buy"]["limit"])) + core.FEE_RESERVE)
    return total


class LateCheckedClient:
    """Last-second rank/quote/time gate in CashCheckedClient's pre-dispatch path."""
    def __init__(self, client, *, journal, provider, clock, start, names, series, side,
                 original_limit, sol_blocked):
        self.client, self.journal = client, journal
        self.provider, self.clock = provider, clock
        self.start, self.names = start, names
        self.series, self.side = series, side
        self.original_limit, self.sol_blocked = D(original_limit), sol_blocked
        self.expected_client_id = None

    def __getattr__(self, attr):
        return getattr(self.client, attr)

    def entry_direction_check(self, ticker, side, window_start_ms):
        base = {"source": "basket_rank_late", "side": side,
                "allowed": False, "reason": "late_rank_unavailable"}
        now = self.clock()
        if (not in_late_window(now, self.start)
                or window_start_ms != self.start * 1000):
            return dict(base, reason="late_entry_time_invalid")
        if self.series == "KXBTC15M" or (self.series == "KXSOL15M" and self.sol_blocked(now)):
            return dict(base, reason="late_entry_time_restricted")
        try:
            permitted, reason = zone2_entry_status(
                self.client, self.journal, ticker,
                expected_client_id=self.expected_client_id)
        except Exception:
            return dict(base, reason="late_rank_unavailable")
        if not permitted:
            return dict(base, reason="late_zone_unavailable", detail=reason)
        try:
            rows = ranked.ranked_observations(
                self.provider.snapshot(self.names, self.start),
                self.names, self.start, self.clock(), allow_partial=True)
        except Exception:
            return base
        top = rows[:MAX_LATE_COINS]
        if not any(r.series == self.series for r in top):
            return dict(base, reason="late_rank_changed")
        row = next(r for r in top if r.series == self.series)
        direction = "YES" if row.return_pct > 0 else "NO" if row.return_pct < 0 else None
        if side != self.side or side != direction:
            return dict(base, reason="late_rank_changed")
        try:
            market = self.client.market(ticker)
            plan = late_plan(market, self.start, self.clock(), self.series, side)
        except Exception:
            plan = None
        if (plan is None or plan["ticker"] != ticker
                or D(plan["limit"]) > self.original_limit):
            return dict(base, reason="late_quote_changed")
        return {"source": "basket_rank_late", "side": side, "allowed": True,
                "reason": "late_rank_aligned", "rank": top.index(row) + 1,
                "return_pct": str(row.return_pct),
                "window_start_ms": window_start_ms}


def run_late_pass(client, journal, series_list, base_budget, max_contracts,
                  live, now, *, provider, clock, sol_blocked):
    checked = clock()
    if not math.isfinite(checked):
        return
    start = int(checked // 900) * 900
    if not in_late_window(checked, start):
        return
    names = tuple(series_list)
    try:
        rows = ranked.ranked_observations(
            provider.snapshot(names, start), names, start, clock(),
            allow_partial=True)
    except Exception as error:
        core.event("M9_LATE_RANK_UNAVAILABLE", **ranked.error_fields(error))
        return

    top_two = rows[:MAX_LATE_COINS]
    eligible = [(i, row) for i, row in enumerate(top_two, 1)
                if row.series != "KXBTC15M"
                and not (row.series == "KXSOL15M" and sol_blocked(clock()))
                and row.return_pct != 0]
    active_budget = core.trade_budget(
        journal.data["streak"], base_budget,
        budget_cycle=journal.data.get("budget_cycle_v2"))
    total_budget = active_budget / D(2)
    spent = half_budget_reservation(journal, start)
    core.event("M9_LATE_BASKET_RANK", entry_zone=LATE_ZONE,
               price_range_cents="89-97", take_profit_cents=98,
               window_start_ms=start * 1000,
               original_top_two=[{"rank": i, "coin": row.series,
                                  "return_pct": str(row.return_pct)}
                                 for i, row in enumerate(top_two, 1)],
               btc_excluded=True, sol_time_restricted=sol_blocked(clock()),
               total_late_budget_dollars=str(total_budget),
               active_budget_dollars=str(active_budget),
               sol_budget_factor=str(core.SOL_BUDGET_FACTOR),
               reserved_late_budget_dollars=str(spent))
    plans = []
    for rank, row in eligible:
        side = "YES" if row.return_pct > 0 else "NO"
        try:
            markets = client.markets(series_ticker=row.series, status="open", limit=100)
        except Exception as error:
            core.event("M9_LATE_MARKET_UNAVAILABLE", series=row.series,
                       **ranked.error_fields(error))
            continue
        for market in markets:
            plan = late_plan(market, start, clock(), row.series, side)
            if plan and not zone2_attempt_exists(journal, plan["ticker"]):
                plans.append((rank, row, plan))
                break
    if not plans:
        return

    for plan_index, (rank, row, plan) in enumerate(plans):
        if not in_late_window(clock(), start):
            return
        ticker = plan["ticker"]
        if zone2_attempt_exists(journal, ticker):
            continue
        available = total_budget - half_budget_reservation(journal, start)
        if available < D(plan["limit"]) + core.FEE_RESERVE:
            return
        try:
            permitted, reason = zone2_entry_status(client, journal, ticker)
            if not permitted:
                core.event("M9_LATE_ZONE_BLOCKED", ticker=ticker, entry_zone=LATE_ZONE,
                           reason=reason)
                continue
            fresh_market = client.market(ticker)
            fresh = late_plan(fresh_market, start, clock(), row.series, plan["side"])
        except Exception as error:
            core.event("M9_LATE_MARKET_UNAVAILABLE", series=row.series,
                       **ranked.error_fields(error))
            continue
        if not fresh:
            continue

        # Share half-budget between the remaining eligible coins; the last
        # coin may consume the residual amount, but never exceed half total.
        remaining_coins = sum(1 for _, _, p in plans[plan_index:]
                              if not zone2_attempt_exists(journal, p["ticker"]))
        target = available / D(max(1, remaining_coins))
        # SOL gets HALF of the Zone 2 budget ceiling as well as Zone 2
        # already using half of the active phase. This cap is cumulative,
        # not a second budget for each attempt or every time rank changes.
        if row.series == "KXSOL15M":
            sol_spent = sum(
                D(str(item["quantity"])) *
                (D(str(item["buy"]["limit"])) + core.FEE_RESERVE)
                for item in journal.data["windows"].values()
                if item.get("window_start_ms") == start * 1000
                and item.get("entry_kind") == LATE_KIND
                and item.get("series") == "KXSOL15M"
                and isinstance(item.get("buy"), dict))
            sol_remaining = max(D(0),
                core.budget_for_series(total_budget, row.series) - sol_spent)
            target = min(target, sol_remaining)
        unit = D(fresh["limit"]) + core.FEE_RESERVE
        quantity = min(max_contracts, int((target / unit).to_integral_value(rounding=ROUND_FLOOR)))
        if quantity <= 0 and row.series != "KXSOL15M":
            quantity = min(max_contracts, int((available / unit).to_integral_value(rounding=ROUND_FLOOR)))
        if quantity <= 0:
            continue
        if not live:
            core.event("M9_LATE_PREVIEW", **fresh, rank=rank, quantity=quantity,
                       late_budget_dollars=str(total_budget))
            continue

        now_submit = clock()
        if not in_late_window(now_submit, start):
            return
        intent = {"client_id": str(uuid.uuid4()), "limit": fresh["limit"],
                  "created_at": now_submit}
        record = {
            "ticker": ticker, "series": row.series, "side": fresh["side"],
            "entry_zone": LATE_ZONE, "take_profit_target": str(LATE_TAKE_PROFIT),
            "window_start_ms": start * 1000, "entry_kind": LATE_KIND,
            "basket_rank_at_entry": rank, "basket_return_pct": str(row.return_pct),
            "quantity": str(quantity), "allocated_dollars": str(quantity * unit),
            "regular_active_budget_at_entry": str(active_budget),
            "sol_budget_factor_at_entry": str(core.SOL_BUDGET_FACTOR)
                if row.series == "KXSOL15M" else "1",
            "budget_phase_at_entry": (journal.data.get("budget_cycle_v2") or {}).get("phase"),
            "buy": intent, "sells": [],
        }
        journal.data["windows"][core.zone_record_key(ticker, LATE_ZONE)] = record
        journal.save()
        validator = LateCheckedClient(
            client, journal=journal, provider=provider, clock=clock, start=start, names=names,
            series=row.series, side=fresh["side"], original_limit=fresh["limit"],
            sol_blocked=sol_blocked)
        state = getattr(journal, "_entry_cash_state", None)
        if state is None:
            state = cash.CashState()
            journal._entry_cash_state = state
        validator.expected_client_id = intent["client_id"]
        protected_client = cash.CashCheckedClient(validator, journal, state)
        core.event("M9_LATE_BUY_SUBMIT", ticker=ticker, entry_zone=LATE_ZONE,
                   take_profit_cents=98, series=row.series,
                   rank=rank, side=fresh["side"], price=fresh["limit"],
                   quantity=quantity, total_late_budget_dollars=str(total_budget),
                   reserved_after_dollars=str(half_budget_reservation(journal, start)))
        try:
            response = protected_client.place_entry(
                ticker, fresh["side"], quantity, D(fresh["limit"]),
                core.parse_time(fresh_market["close_time"]),
                submit_before=start + LATE_END_SECONDS,
                client_order_id=intent["client_id"], ioc=True)
        except cash.EntryNotSubmitted as error:
            # Definitive not-dispatched / venue rejection is durable. Preserve
            # its reserved amount for the rest of this window.
            core.event("M9_LATE_ENTRY_NOT_SENT", ticker=ticker, reason=str(error))
            continue
        except Exception as error:
            core.event("M9_LATE_SUBMISSION_UNCERTAIN", ticker=ticker,
                       **ranked.error_fields(error))
            raise  # Unknown acceptance means NEVER attempt another coin.
        order = response.get("order", response)
        if not order.get("order_id"):
            core.event("M9_LATE_ACK_UNRESOLVED", ticker=ticker)
            return  # Uncertain acceptance: block additional buys this poll.
        intent["order_id"] = order["order_id"]
        journal.save()
        try:
            core.reconcile_exit(client, journal, record, clock())
            core.verify_completed_trade(client, journal, record, clock())
            core.apply_new_outcomes(journal)
        except Exception as error:
            core.event("M9_LATE_CONFIRMATION_PENDING", ticker=ticker,
                       **ranked.error_fields(error))
        # One successful or uncertain entry per poll. Subsequent polls
        # may allocate the remaining HALF-budget to the second-ranked coin.
        return
