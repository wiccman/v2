"""Cash/rejection safety adapter for the existing performance-ranked bot.

No new signal, transfers, retries, or price chasing.
Use aggregate account cash as preflight and let Kalshi's
order endpoint authoritatively reject exchange-specific shortfalls.
Shard breakdown is diagnostic only; never move funds automatically. Original core owns timing,
ranking, sizing, exits and order IDs. Only definite pre-dispatch skips and the
exact HTTP 400 / insufficient_balance rejection receive a terminal no-fill.
All other submission errors keep the original uncertain-order reservation.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from decimal import Decimal as D

import single_coin_minute9 as core
import performance_ranked_minute9 as ranked
from kalshi import KalshiAPIError

POLICY = "aggregate-preflight-venue-rejection-v2"
ORIGINAL_RUN_PASS = core.run_pass
ORIGINAL_SLOT_RELEASED = core.entry_slot_released


class EntryNotSubmitted(RuntimeError):
    """An entry was positively not sent, or explicitly rejected by exchange."""


class CashUnavailable(ValueError):
    """Missing cash information is not a zero balance."""


def number(value):
    if isinstance(value, bool):
        raise CashUnavailable("invalid_cash_value")
    try:
        value = D(str(value))
    except Exception as error:
        raise CashUnavailable("invalid_cash_value") from error
    if not value.is_finite() or value < 0:
        raise CashUnavailable("invalid_cash_value")
    return value


def cash_snapshot(payload):
    """Get Balance: totals may be cents; per-exchange balances are dollars."""
    if not isinstance(payload, dict):
        raise CashUnavailable("invalid_balance_response")
    cents, dollars = payload.get("balance"), payload.get("balance_dollars")
    if dollars is None and cents is None:
        raise CashUnavailable("total_cash_missing")
    total = number(dollars) if dollars is not None else number(cents) / 100
    if dollars is not None and cents is not None and abs(total-number(cents)/100) >= D(".01"):
        raise CashUnavailable("conflicting_total_cash")
    raw = payload.get("balance_breakdown")
    if raw is None:
        # Restricted-subaccount keys can omit the breakdown. No assumption that
        # their aggregate cash belongs to the market's exchange is permissible.
        return total, None
    if not isinstance(raw, list):
        raise CashUnavailable("invalid_cash_breakdown")
    shards = {}
    for row in raw:
        if not isinstance(row, dict):
            raise CashUnavailable("invalid_cash_breakdown")
        index = row.get("exchange_index")
        if type(index) is not int or index < 0 or index in shards:
            raise CashUnavailable("ambiguous_cash_breakdown")
        shards[index] = number(row.get("balance"))
    return total, shards


def release_proven_no_order(record):
    proof = record.get("entry_no_order_proof") or {}
    intent = record.get("buy") or {}
    if (record.get("outcome", {}).get("status") != "no_fill"
            or not intent.get("client_id") or intent.get("order_id")
            or record.get("sells") or proof.get("client_id") != intent["client_id"]
            or proof.get("ticker") != record.get("ticker")
            or proof.get("window_start_ms") != record.get("window_start_ms")):
        return False
    if proof.get("kind") == "not_dispatched":
        return proof.get("reason") in {"insufficient_market_cash", "insufficient_total_cash", "cash_check_unavailable", "entry_deadline_passed"}
    return (proof.get("kind") == "exchange_rejected"
            and proof.get("http_status") == 400
            and proof.get("api_code") == "insufficient_balance")


def entry_slot_released(record):
    return release_proven_no_order(record) or ORIGINAL_SLOT_RELEASED(record)


def finish_no_order(journal, record, *, kind, reason, now, http_status=None, api_code=None):
    if record["buy"].get("order_id") or record.get("sells"):
        raise RuntimeError("Cannot mark an identified or managed order unsubmitted")
    record["entry_no_order_proof"] = {
        "kind": kind, "reason": reason, "ticker": record["ticker"],
        "client_id": record["buy"]["client_id"], "window_start_ms": record["window_start_ms"],
        "http_status": http_status, "api_code": api_code, "recorded_at": now,
    }
    record["outcome"] = {"status": "no_fill", "reason": reason,
                         "completed_at": now, "counted": False}
    journal.save()  # Both the proof and terminal result survive a restart.


class CashState:
    """Read-only cash cache; never increase sizing or move money between shards."""
    def __init__(self):
        self.last_attempt = float("-inf")
        self.last_success = float("-inf")
        self.snapshot = None
        self.diagnostic_done = False

    def read(self, client, now):
        if self.snapshot is not None and 0 <= now-self.last_success < 1:
            return self.snapshot
        if now-self.last_attempt < 1:
            raise CashUnavailable("cash_read_cooldown")
        self.last_attempt = now
        self.snapshot = None  # A failed read must not leave stale cash usable.
        payload = client.balance()
        total, shards = cash_snapshot(payload)
        self.snapshot = (total, shards)
        self.last_success = now
        self.diagnostic_done = True
        core.event("M9_CASH_SNAPSHOT", total_cash_dollars=str(total),
                   exchange_balances=([{"exchange_index": i, "cash_dollars": str(v)}
                                       for i, v in sorted(shards.items())] if shards is not None else None),
                   scope="same_API_key_default_account", policy=POLICY)
        return self.snapshot


class CashCheckedClient:
    def __init__(self, client, journal, state):
        self.client, self.journal, self.state = client, journal, state

    def __getattr__(self, name):
        return getattr(self.client, name)

    def place_entry(self, ticker, prediction, quantity, outcome_price, expiration_time,
                    *, submit_before=None, client_order_id=None, ioc=False):
        record = self.journal.data["windows"].get(ticker)
        if (not record or record.get("buy", {}).get("client_id") != client_order_id
                or record.get("side") != prediction or D(record["quantity"]) != D(quantity)
                or D(record["buy"]["limit"]) != D(outcome_price)):
            raise RuntimeError("Entry guard requires the exact durable order intent")
        now = core.time.time()
        try:
            market = self.client.market(ticker)
            if market.get("ticker") != ticker:
                raise CashUnavailable("cash_market_mismatch")
            index = market.get("exchange_index")
            if type(index) is not int or index < 0:
                raise CashUnavailable("market_exchange_index_missing")
            total, shards = self.state.read(self.client, now)
            # Shard balances are diagnostic. Kalshi's order API decides
            # whether the aggregate cash is spendable on this market.
            market_cash = shards.get(index) if shards is not None else None
            available = total
            required = D(quantity) * (D(outcome_price) + core.FEE_RESERVE)
            if not required.is_finite() or required <= 0:
                raise CashUnavailable("invalid_entry_cost")
        except Exception as error:
            finish_no_order(self.journal, record, kind="not_dispatched",
                            reason="cash_check_unavailable", now=core.time.time())
            core.event("M9_ENTRY_NOT_SENT", ticker=ticker, reason="cash_check_unavailable",
                       **ranked.error_fields(error))
            raise EntryNotSubmitted("cash_check_unavailable") from error
        record["cash_preflight"] = {"exchange_index": index, "total_cash_dollars": str(total),
                                    "market_cash_dollars": str(market_cash) if market_cash is not None else None,
                                    "cash_check_scope": "aggregate_account_cash",
                                    "required_with_fee_reserve_dollars": str(required)}
        core.event("M9_CASH_CHECK", ticker=ticker, **record["cash_preflight"])
        if available < required:
            finish_no_order(self.journal, record, kind="not_dispatched",
                            reason="insufficient_total_cash", now=core.time.time())
            core.event("M9_ENTRY_NOT_SENT", ticker=ticker, reason="insufficient_total_cash",
                       **record["cash_preflight"])
            raise EntryNotSubmitted("insufficient_total_cash")
        deadline = min(expiration_time, submit_before if submit_before is not None else expiration_time)
        if not math.isfinite(deadline) or core.time.time() >= deadline:
            finish_no_order(self.journal, record, kind="not_dispatched",
                            reason="entry_deadline_passed", now=core.time.time())
            raise EntryNotSubmitted("entry_deadline_passed")
        self.journal.save()
        try:
            response = self.client.place_entry(ticker, prediction, quantity, outcome_price,
                expiration_time, submit_before=submit_before, client_order_id=client_order_id, ioc=ioc)
        except KalshiAPIError as error:
            self.state.snapshot = None
            if error.status_code != 400 or error.code != "insufficient_balance":
                raise  # A timeout, other code, or server error is not rejection proof.
            finish_no_order(self.journal, record, kind="exchange_rejected",
                            reason="insufficient_balance", now=core.time.time(),
                            http_status=400, api_code="insufficient_balance")
            core.event("M9_ENTRY_REJECTED", ticker=ticker, http_status=400,
                       api_code="insufficient_balance", **record["cash_preflight"])
            raise EntryNotSubmitted("exchange_rejected_insufficient_balance") from error
        self.state.snapshot = None  # Never reuse cash after an order could fill.
        return response


def run_cash_checked_pass(client, journal, series_list, base_budget, max_contracts,
                          live, now, max_entries_per_window=1):
    state = getattr(journal, "_entry_cash_state", None)
    if state is None:
        state = CashState()
        journal._entry_cash_state = state
    guarded = CashCheckedClient(client, journal, state) if live else client
    try:
        return ORIGINAL_RUN_PASS(guarded, journal, series_list, base_budget, max_contracts,
                                 live, now, max_entries_per_window)
    except EntryNotSubmitted:
        core.apply_new_outcomes(journal)  # no_fill does not touch the win streak
        # No immediate new POST after a rejection. A later normal poll can
        # reconsider another coin only with fresh available-cash evidence.
        return None
    finally:
        # One startup GET, after owned-position exit handling, provides the
        # cash breakdown even when no entry is eligible. Never submit an order.
        if live and not state.diagnostic_done:
            state.diagnostic_done = True
            try:
                state.read(client, core.time.time())
            except Exception as error:
                core.event("M9_CASH_DIAGNOSTIC_UNAVAILABLE", **ranked.error_fields(error))


@contextmanager
def installed_guard():
    previous_slot, previous_pass = core.entry_slot_released, ranked._CORE_RUN_PASS
    core.entry_slot_released = entry_slot_released
    ranked._CORE_RUN_PASS = run_cash_checked_pass
    try:
        yield
    finally:
        core.entry_slot_released, ranked._CORE_RUN_PASS = previous_slot, previous_pass


def main(argv=None):
    with installed_guard():
        core.event("M9_CASH_GUARD_READY", policy=POLICY, transfers_enabled=False,
                   automatic_retries=False, budget_override=False)
        return ranked.main(argv)


if __name__ == "__main__":
    main()
