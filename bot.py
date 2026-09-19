import argparse, csv, json, os, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv
from kalshi import KalshiClient, KalshiAPIError
from entry_policy import initialize as initialize_budget, reserve as reserve_entry, market_budget
from take_profit import TakeProfitMonitor
from price_pairs import parse_pairs
from strategy import strike_ruler, live_confidence, average_open_price, average_prediction_confidence, spot_is_above_strike, seconds_from_minutes

load_dotenv()
if os.getenv("MODE", "live").lower() != "live" or os.getenv("KALSHI_ENV", "production").lower() != "production":
    raise SystemExit("This package supports live Kalshi production only")

ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
EXECUTION_STRATEGY = os.getenv("EXECUTION_STRATEGY", "strike_ruler").lower()
if EXECUTION_STRATEGY != "strike_ruler":
    raise SystemExit("Only EXECUTION_STRATEGY=strike_ruler is supported")
ENTRY_EXIT_PAIRS = parse_pairs(os.getenv("ENTRY_EXIT_PAIRS_CENTS", "32:39,39:46"))
OPENING_BIAS_ENABLED = os.getenv("OPENING_BIAS_ENABLED", "true").lower() == "true"
OPENING_BIAS_PAIR = parse_pairs(os.getenv("OPENING_BIAS_PAIR_CENTS", "52:60"))
OPENING_WINDOW = seconds_from_minutes(os.getenv("OPENING_WINDOW_MINUTES", "2"))
ALL_ENTRY_EXIT_PAIRS = dict(sorted({**ENTRY_EXIT_PAIRS, **OPENING_BIAS_PAIR}.items()))
# Compatibility values for the retired synchronous single-tier helpers only.
ENTRY_PRICE, EXIT_PRICE = next(iter(ENTRY_EXIT_PAIRS.items()))
MARKET_BUDGET = market_budget()
CANCEL_AFTER = 360
BUDGET = Decimal(os.getenv("ENTRY_BUDGET_DOLLARS", "0.77"))
MAX_BUYS = int(os.getenv("MAX_PURCHASES_PER_MARKET", "7"))
INTERVAL = int(os.getenv("ENTRY_INTERVAL_SECONDS", "7"))
START = seconds_from_minutes(os.getenv("ENTRY_START_MINUTE", "2"))
END = min(seconds_from_minutes(os.getenv("ENTRY_END_MINUTE", "5")), 300)
PREDICTION_MINUTES = (2, 4, 6)
PREDICTION_GRACE_SECONDS = 15
PREDICTION_SECONDS = tuple(minute * 60 for minute in PREDICTION_MINUTES)
SPOT_ENTRY_WINDOW = min(seconds_from_minutes(os.getenv("SPOT_ENTRY_WINDOW_MINUTES", "2")), END)
SPOT_ENTRY_THRESHOLD = Decimal(os.getenv("SPOT_ENTRY_THRESHOLD_DOLLARS", "80"))
TAKE_PROFIT_RETRY_SECONDS = int(os.getenv("TAKE_PROFIT_RETRY_SECONDS", "60"))
DUAL_LIMIT_BUYS_ENABLED = os.getenv("DUAL_LIMIT_BUYS_ENABLED", "true").lower() == "true"
HISTORICAL_STRIKE_ENABLED = os.getenv("HISTORICAL_STRIKE_ENABLED", "true").lower() == "true"
HISTORICAL_STRIKE_COUNT = int(os.getenv("HISTORICAL_STRIKE_COUNT", "3"))
HISTORICAL_STRIKE_TOUCH_DOLLARS = Decimal(os.getenv("HISTORICAL_STRIKE_TOUCH_DOLLARS", "25"))
ABS_GAP_AVG = Decimal(os.getenv("ABSOLUTE_GAP_AVERAGE", "59.58"))
STATE = Path(os.getenv("STATE_PATH", "/data/state.json"))
LOG = Path(os.getenv("LOG_PATH", "/data/trades.csv"))
client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""), os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""))
EXIT_MONITOR = None

def parse_time(value): return datetime.fromisoformat(value.replace("Z", "+00:00"))
def load_state():
    # Never turn a lost spending ledger into a new allowance.
    if not STATE.exists():
        raise RuntimeError("STATE_MISSING: restore state.json before starting; no automatic reset")
    state = json.loads(STATE.read_text())
    if not isinstance(state, dict) or not isinstance(state.get("markets"), dict):
        raise ValueError("STATE_INVALID: expected a markets object; restore the saved ledger")
    for record in state["markets"].values():
        if not isinstance(record, dict):
            raise ValueError("STATE_INVALID: market record must be an object")
        if "entry_intents" in record:
            intents = record["entry_intents"]
            if not isinstance(intents, list):
                raise ValueError("STATE_INVALID: entry ledger must be a list")
            for intent in intents:
                if not isinstance(intent, dict) or "reserved_dollars" not in intent:
                    raise ValueError("STATE_INVALID: entry reservation is missing")
                amount = Decimal(str(intent["reserved_dollars"]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("STATE_INVALID: entry reservation must be finite and nonnegative")
    return state

def validate_storage():
    if os.getenv("RAILWAY_ENVIRONMENT_ID"):
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        if not mount or not os.path.ismount(mount) or not all(
            p.resolve().is_relative_to(Path(mount).resolve()) for p in (STATE, LOG)
        ):
            raise RuntimeError("STATE_VOLUME_REQUIRED: state and log must be on the persistent Railway volume")
    load_state()

def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(STATE)
def write_log(event, ticker="", **values):
    fields = ["time_utc", "ticker", "event", "prediction", "confidence", "price", "quantity", "details"]
    row = {field: "" for field in fields}
    row.update(time_utc=datetime.now(timezone.utc).isoformat(), ticker=ticker, event=event)
    row.update(values)
    # Emit before file I/O so even a broken volume is visible in Railway logs.
    print(json.dumps(row), flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    exists = LOG.exists()
    with LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def active_market(now):
    for market in client.markets(series_ticker="KXBTC15M", status="open", limit=100):
        closed = parse_time(market["close_time"])
        started = closed - timedelta(minutes=15)
        if started <= now < closed: return market, started, closed
    return None, None, None

def completed_values(started, field, count):
    expected = [started - timedelta(minutes=15 * offset) for offset in reversed(range(count))]
    found = {}
    for market in client.markets(series_ticker="KXBTC15M", status="settled", limit=100):
        closed = parse_time(market["close_time"])
        value = market.get(field)
        if closed not in expected or value in (None, ""):
            continue
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0:
            raise RuntimeError("DATA UNAVAILABLE: invalid finalized lookback price")
        if closed in found and found[closed] != value:
            raise RuntimeError("DATA UNAVAILABLE: conflicting lookback values")
        found[closed] = value
    missing = [close.isoformat() for close in expected if close not in found]
    if missing:
        raise RuntimeError(f"DATA UNAVAILABLE: exact prior periods not finalized: {missing}")
    return [found[close] for close in expected]

def prior_three(started):
    return completed_values(started, "expiration_value", 3)

def prior_strikes(started, count=HISTORICAL_STRIKE_COUNT):
    return completed_values(started, "floor_strike", count)

def quotes(market, prediction):
    if prediction == "YES": return Decimal(market["yes_ask_dollars"]), Decimal(market["yes_bid_dollars"])
    return Decimal(market["no_ask_dollars"]), Decimal(market["no_bid_dollars"])

def position(ticker):
    for item in client.positions(ticker):
        if item.get("ticker") == ticker: return Decimal(str(item.get("position_fp", "0")))
    return Decimal("0")

def entry_price_allowed(base_confidence, price):
    return base_confidence in ("HIGH", "MODERATE") and Decimal(str(price)) in ENTRY_EXIT_PAIRS

def manage_exit(record, ticker, market, signal, closed, reserved_quantity=Decimal("0"), excluded_order_ids=None):
    held = position(ticker)
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    take_profit_order_id = record.get("take_profit_order_id")
    if held == 0:
        if take_profit_order_id in resting:
            client.cancel(take_profit_order_id, ticker)
            write_log("CANCEL_TAKE_PROFIT", ticker, details=take_profit_order_id)
        changed = any(record.pop(key, None) is not None for key in (
            "take_profit_order_id", "take_profit_side", "take_profit_quantity", "take_profit_target",
            "take_profit_rejected_side", "take_profit_rejected_quantity",
            "take_profit_rejected_target", "take_profit_retry_after",
        ))
        return changed
    side = "YES" if held > 0 else "NO"; _, bid = quotes(market, side)
    quantity = max(Decimal("0"), abs(held) - Decimal(str(reserved_quantity)))
    if quantity == 0:
        if take_profit_order_id in resting:
            client.cancel(take_profit_order_id, ticker)
            write_log("CANCEL_TAKE_PROFIT", ticker, prediction=side, details=take_profit_order_id)
        changed = any(record.pop(key, None) is not None for key in (
            "take_profit_order_id", "take_profit_side", "take_profit_quantity", "take_profit_target",
            "take_profit_rejected_side", "take_profit_rejected_quantity",
            "take_profit_rejected_target", "take_profit_retry_after",
        ))
        return changed
    target = EXIT_PRICE
    # Retire legacy resting exits before switching to monitored IOC execution.
    if take_profit_order_id in resting:
        if not cancel_confirmed(take_profit_order_id, ticker):
            return False
        record.pop("take_profit_order_id", None)
        write_log("CANCEL_LEGACY_TAKE_PROFIT", ticker, details=take_profit_order_id)
        return True
    if bid < target:
        return False
    rejected_matches = (
        record.get("take_profit_execution_version") == 2
        and record.get("take_profit_rejected_side") == side
        and Decimal(str(record.get("take_profit_rejected_quantity", "0"))) == quantity
        and Decimal(str(record.get("take_profit_rejected_target", "-1"))) == target
    )
    if rejected_matches and time.time() < float(record.get("take_profit_retry_after", 0)):
        return False
    signed_quantity = quantity if side == "YES" else -quantity
    record["take_profit_execution_version"] = 2
    try:
        result = client.place_take_profit(ticker, signed_quantity, target, closed.timestamp())
    except Exception as error:
        record["take_profit_rejected_side"] = side
        record["take_profit_rejected_quantity"] = str(quantity)
        record["take_profit_rejected_target"] = str(target)
        record["take_profit_retry_after"] = time.time() + TAKE_PROFIT_RETRY_SECONDS
        write_log(
            "TAKE_PROFIT_REJECTED", ticker, prediction=side, price=str(target),
            quantity=str(quantity), details=repr(error),
        )
        return True
    order_id = result.get("order_id")
    if not order_id:
        record["take_profit_rejected_side"] = side
        record["take_profit_rejected_quantity"] = str(quantity)
        record["take_profit_rejected_target"] = str(target)
        record["take_profit_retry_after"] = time.time() + TAKE_PROFIT_RETRY_SECONDS
        write_log("TAKE_PROFIT_REJECTED", ticker, prediction=side, price=str(target), quantity=str(quantity), details=json.dumps(result))
        return True
    record["take_profit_order_id"] = order_id
    record["take_profit_sizing_version"] = 1
    record["take_profit_side"] = side
    record["take_profit_quantity"] = str(quantity)
    record["take_profit_target"] = str(target)
    for key in (
        "take_profit_rejected_side", "take_profit_rejected_quantity",
        "take_profit_rejected_target", "take_profit_retry_after",
    ):
        record.pop(key, None)
    details = {"target": str(target), "target_mode": "absolute_outcome_price", "order": result}
    write_log("TAKE_PROFIT_IOC", ticker, prediction=side, confidence=signal.get("live_confidence", ""), price=str(target), quantity=str(quantity), details=json.dumps(details))
    return True

def cancel_confirmed(order_id, ticker):
    try:
        result = client.cancel(order_id, ticker)
        if result.get("order_id") == order_id and "reduced_by" in result:
            write_log("CANCEL_ENTRY_CONFIRMED", ticker, details=order_id)
            return True
    except Exception as error:
        write_log("ENTRY_CANCEL_RETRY", ticker, details=f"{order_id}: {error!r}")
    # A 404, timeout, or incomplete response is not proof of cancellation.
    try:
        return client.order(order_id).get("status") in {"canceled", "executed", "expired"}
    except Exception as error:
        write_log("ENTRY_STATUS_RETRY", ticker, details=f"{order_id}: {error!r}")
        return False


def cancel_entries(record, ticker):
    for order_id in list(record.get("orders", [])):
        if cancel_confirmed(order_id, ticker):
            record["orders"].remove(order_id)


def entry_deadline(closed):
    return closed.timestamp() - 900 + END


def cancellation_deadline(closed):
    return closed.timestamp() - 900 + CANCEL_AFTER


def funded_entry(record, state, ticker, side, price, closed, kind, now_timestamp=None, submit_before=None, order_budget=None, cancel_at=None):
    if EXIT_MONITOR is not None and not EXIT_MONITOR.healthy:
        write_log("ENTRY_WAIT_TAKE_PROFIT", ticker, details="Independent exit monitor is not healthy")
        return {}, Decimal("0")
    if Decimal(str(price)) not in ALL_ENTRY_EXIT_PAIRS:
        raise ValueError("Entry price must match a configured fixed entry limit")
    if any(not i.get("entry_closed") and Decimal(str(i.get("price", "-1"))) not in ALL_ENTRY_EXIT_PAIRS
           for i in record.get("entry_intents", [])):
        return {}, Decimal("0")  # Reconcile old-price orders before adding exposure.
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    cutoff = min(entry_deadline(closed), submit_before or entry_deadline(closed))
    if now_timestamp >= cutoff:
        return {}, Decimal("0")
    cancel_at = cancellation_deadline(closed) if cancel_at is None else cancel_at
    intent = reserve_entry(record, side, price, BUDGET if order_budget is None else order_budget,
                           MARKET_BUDGET, cancel_at, kind)
    if intent is None:
        return {}, Decimal("0")
    intent["exit_target"] = str(ALL_ENTRY_EXIT_PAIRS[Decimal(str(price))])
    save_state(state)  # Persist allowance and client ID before any exchange request.
    quantity = Decimal(intent["quantity"])
    try:
        result = client.place_entry(ticker, side, quantity, price, intent["cancel_at"],
                                    submit_before=cutoff, client_order_id=intent["client_id"])
    except KalshiAPIError as error:
        if error.status_code in {400, 401, 403, 404, 422, 429}:
            intent["entry_closed"] = True
        save_state(state)
        raise
    if result.get("order_id"):
        intent["order_id"] = result["order_id"]
    elif not result:
        intent["entry_closed"] = True  # Locally refused at the submit deadline.
    save_state(state)
    if EXIT_MONITOR is not None:
        EXIT_MONITOR.wake()
    write_log("ENTRY_BUDGET", ticker, details=json.dumps({
        "cap": str(MARKET_BUDGET), "reserved": str(sum(Decimal(i["reserved_dollars"]) for i in record["entry_intents"])),
        "kind": kind, "client_id": intent["client_id"], "cancel_at": intent["cancel_at"],
    }))
    return result, quantity


def paired_entries(record, state, ticker, side, closed, kind, now_timestamp=None, submit_before=None):
    """Split the existing per-trigger principal across both levels, not double it."""
    for price in ENTRY_EXIT_PAIRS:
        result, quantity = funded_entry(record, state, ticker, side, price, closed, kind,
            now_timestamp, submit_before, order_budget=BUDGET / len(ENTRY_EXIT_PAIRS))
        yield price, result, quantity


def reconcile_entries(state, now_timestamp=None):
    """Run before market discovery/signals, including markets from earlier cycles."""
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    for ticker, record in state.get("markets", {}).items():
        pending = [i for i in record.get("entry_intents", []) if not i.get("entry_closed")]
        legacy_pending = record.get("orders") or record.get("dual_limit_orders") or any(
            i.get("order_id") and not i.get("entry_closed") for i in record.get("historical_strike_orders", []))
        if not pending and not legacy_pending:
            continue
        try:
            if "entry_cancel_at" not in record:
                record["entry_cancel_at"] = cancellation_deadline(parse_time(client.market(ticker)["close_time"]))
                save_state(state)
            # Recover ambiguous POSTs by the persisted client ID, never resubmit.
            missing = [i for i in pending if not i.get("order_id")]
            if missing:
                try:
                    by_client_id = {o.get("client_order_id"): o for o in client.all_orders(ticker)}
                    for item in missing:
                        remote = by_client_id.get(item["client_id"])
                        if remote:
                            item["order_id"] = remote["order_id"]
                            save_state(state)
                except Exception as error:
                    write_log("ENTRY_RECOVERY_RETRY", ticker, details=repr(error))
            # The entry acknowledgement itself may have been saved immediately
            # before a crash interrupted the route-specific inventory update.
            for item in record.get("entry_intents", []):
                if item["kind"] == "historical" and item.get("order_id") and not any(o.get("order_id") == item["order_id"] for o in record.get("historical_strike_orders", [])):
                    record.setdefault("historical_strike_orders", []).append({
                        "order_id": item["order_id"], "side": item["side"],
                        "cancel_at": item["cancel_at"], "entry_closed": item.get("entry_closed", False)})
                    save_state(state)
            # Old budgets cannot be reconstructed reliably. Cancel their unfilled
            # remainder on upgrade and resume entries only in a clean market.
            if now_timestamp < record["entry_cancel_at"] and not record.get("entry_budget_legacy"):
                # On a price-policy upgrade, cancel tracked incompatible buys
                # immediately; keep their spending reservations across restarts.
                ids = {i["order_id"] for i in pending if i.get("order_id")
                       and Decimal(str(i.get("price", "-1"))) not in ALL_ENTRY_EXIT_PAIRS}
                ids |= {i["order_id"] for i in pending if i.get("order_id") and now_timestamp >= i.get("cancel_at", float("inf"))}
            else:
                ids = set(record.get("orders", [])) | set(record.get("dual_limit_orders", []))
                ids |= {i["order_id"] for i in pending if i.get("order_id")}
                ids |= {i["order_id"] for i in record.get("historical_strike_orders", []) if i.get("order_id") and not i.get("entry_closed")}
            for order_id in sorted(ids):
                if not cancel_confirmed(order_id, ticker):
                    continue
                for key in ("orders", "dual_limit_orders"):
                    record[key] = [oid for oid in record.get(key, []) if oid != order_id]
                for item in record.get("entry_intents", []) + record.get("historical_strike_orders", []):
                    if item.get("order_id") == order_id:
                        item["entry_closed"] = True
                save_state(state)
        except Exception as error:
            # Keep retrying, but let other markets and position exits progress.
            write_log("ENTRY_RECONCILE_RETRY", ticker, details=repr(error))


def place_dual_limit_buys(record, ticker, closed, now_timestamp=None, *, state):
    """Post fixed-price entries sharing one budget and the six-minute expiry."""
    initialize_budget(record)
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    cancel_at = cancellation_deadline(closed)
    if now_timestamp >= entry_deadline(closed):
        return False
    record["dual_limit_cancel_at"] = cancel_at
    record.setdefault("dual_limit_orders", [])
    changed = True
    for side in ("YES", "NO"):
        for price in ENTRY_EXIT_PAIRS:
            quantity = Decimal("0")
            try:
                result, quantity = funded_entry(record, state, ticker, side, price, closed, "dual", now_timestamp,
                    order_budget=BUDGET / (2 * len(ENTRY_EXIT_PAIRS)))
            except Exception as error:
                write_log("DUAL_LIMIT_REJECTED", ticker, prediction=side, price=str(price),
                          quantity=str(quantity), details=repr(error))
                continue
            order_id = result.get("order_id")
            if order_id:
                record["dual_limit_orders"].append(order_id)
                write_log("DUAL_LIMIT_RESTING", ticker, prediction=side, price=str(price),
                    quantity=str(quantity), details=json.dumps({"cancel_at": cancel_at, "order": result}))
    return changed

def cancel_expired_dual_limits(record, ticker, now_timestamp=None):
    order_ids = list(record.get("dual_limit_orders", []))
    if not order_ids:
        return False
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    if now_timestamp < float(record.get("dual_limit_cancel_at", 0)):
        return False
    for order_id in order_ids:
        if cancel_confirmed(order_id, ticker):
            record["dual_limit_orders"].remove(order_id)
    record["dual_limit_cancelled"] = not record["dual_limit_orders"]
    return True


def strike_reaction_side(reference_spot, strike):
    reference_spot = Decimal(str(reference_spot)); strike = Decimal(str(strike))
    if reference_spot < strike:
        return "NO"
    if reference_spot > strike:
        return "YES"
    return None

def place_historical_strike_entries(record, ticker, spot, closed, now_timestamp=None, *, state):
    initialize_budget(record)
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    if now_timestamp >= entry_deadline(closed):
        return False
    spot = Decimal(str(spot))
    previous_spot = Decimal(str(record.get("historical_last_spot", spot)))
    record["historical_last_spot"] = str(spot)
    triggered = set(record.get("historical_triggered_strikes", []))
    record.setdefault("historical_strike_orders", [])
    changed = False
    for raw_strike in record.get("historical_strikes", []):
        strike = Decimal(str(raw_strike)); strike_key = str(strike)
        if strike_key in triggered or abs(spot - strike) > HISTORICAL_STRIKE_TOUCH_DOLLARS:
            continue
        side = strike_reaction_side(previous_spot, strike) or strike_reaction_side(spot, strike)
        if side is None:
            continue
        triggered.add(strike_key)
        record["historical_triggered_strikes"] = sorted(triggered)
        cancel_at = cancellation_deadline(closed)
        for price in ENTRY_EXIT_PAIRS:
            quantity = Decimal("0")
            order_record = {
                "strike": strike_key, "side": side, "quantity": str(quantity),
                "entry_price": str(price), "exit_target": str(ENTRY_EXIT_PAIRS[price]), "cancel_at": cancel_at,
            }
            try:
                result, quantity = funded_entry(record, state, ticker, side, price, closed, "historical", now_timestamp,
                    order_budget=BUDGET / len(ENTRY_EXIT_PAIRS))
                order_record["quantity"] = str(quantity)
            except Exception as error:
                order_record["rejected"] = repr(error)
                record["historical_strike_orders"].append(order_record)
                write_log(
                    "HISTORICAL_STRIKE_REJECTED", ticker, prediction=side,
                    price=str(price), quantity=str(quantity),
                    details=json.dumps(order_record),
                )
                changed = True
                continue
            order_id = result.get("order_id")
            if order_id:
                order_record["order_id"] = order_id
            record["historical_strike_orders"].append(order_record)
            details = {**order_record, "spot": str(spot), "order": result}
            write_log(
                "HISTORICAL_STRIKE_RESTING", ticker, prediction=side,
                price=str(price), quantity=str(quantity),
                details=json.dumps(details),
            )
            changed = True
    return changed

def cancel_expired_historical_entries(record, ticker, now_timestamp=None):
    orders = record.get("historical_strike_orders", [])
    pending = [item for item in orders if item.get("order_id") and not item.get("entry_closed")]
    if not pending:
        return False
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    due = [item for item in pending if now_timestamp >= float(item.get("cancel_at", 0))]
    if not due:
        return False
    for item in due:
        if cancel_confirmed(item["order_id"], ticker):
            item["entry_closed"] = True
    return True


def historical_inventory(record, ticker, held):
    entries = record.get("historical_strike_orders", [])
    take_profits = record.get("historical_take_profit_orders", [])
    entry_ids = {item.get("order_id") for item in entries if item.get("order_id")}
    exit_ids = {item.get("order_id") for item in take_profits if item.get("order_id")}
    excluded = entry_ids | exit_ids
    if held == 0 or not entry_ids:
        return Decimal("0"), excluded, None
    side = "YES" if held > 0 else "NO"
    side_entry_ids = {item.get("order_id") for item in entries if item.get("side") == side}
    side_exit_ids = {item.get("order_id") for item in take_profits if item.get("side") == side}
    entered = Decimal("0"); exited = Decimal("0")
    fills = client.fills(ticker)
    for fill in fills:
        count = Decimal(str(fill.get("count_fp") or fill.get("count") or "0"))
        if fill.get("order_id") in side_entry_ids:
            entered += count
        elif fill.get("order_id") in side_exit_ids:
            exited += count
    strategy_fills = [fill for fill in fills if fill.get("order_id") in excluded]
    average_entry = average_open_price(strategy_fills, side)
    return min(abs(held), max(Decimal("0"), entered - exited)), excluded, average_entry

def manage_historical_take_profit(record, ticker, held, reserved_quantity, closed, average_entry=None):
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    active_id = record.get("historical_take_profit_order_id")
    if held == 0 or reserved_quantity <= 0:
        if active_id in resting:
            client.cancel(active_id, ticker)
            write_log("CANCEL_HISTORICAL_TAKE_PROFIT", ticker, details=active_id)
        changed = any(record.pop(key, None) is not None for key in (
            "historical_take_profit_order_id", "historical_take_profit_side",
            "historical_take_profit_quantity", "historical_take_profit_target",
        ))
        return changed
    side = "YES" if held > 0 else "NO"
    target = EXIT_PRICE
    if active_id in resting:
        if not cancel_confirmed(active_id, ticker):
            return False
        record.pop("historical_take_profit_order_id", None)
        write_log("CANCEL_LEGACY_HISTORICAL_TAKE_PROFIT", ticker, details=active_id)
        return True
    _, bid = quotes(client.market(ticker), side)
    if bid < target:
        return False
    signed_quantity = reserved_quantity if side == "YES" else -reserved_quantity
    try:
        result = client.place_take_profit(ticker, signed_quantity, target, closed.timestamp())
    except Exception as error:
        write_log(
            "HISTORICAL_TAKE_PROFIT_REJECTED", ticker, prediction=side,
            price=str(target), quantity=str(reserved_quantity), details=repr(error),
        )
        return False
    order_id = result.get("order_id")
    if not order_id:
        write_log(
            "HISTORICAL_TAKE_PROFIT_REJECTED", ticker, prediction=side,
            price=str(target), quantity=str(reserved_quantity), details=json.dumps(result),
        )
        return False
    record.setdefault("historical_take_profit_orders", []).append({"order_id": order_id, "side": side})
    record["historical_take_profit_order_id"] = order_id
    record["historical_take_profit_side"] = side
    record["historical_take_profit_quantity"] = str(reserved_quantity)
    record["historical_take_profit_target"] = str(target)
    write_log(
        "HISTORICAL_TAKE_PROFIT_IOC", ticker, prediction=side,
        price=str(target), quantity=str(reserved_quantity), details=json.dumps(result),
    )
    return True

def update_prediction(record, ticker, current, elapsed):
    changed = False
    while len(record["predictions"]) < len(PREDICTION_SECONDS):
        index = len(record["predictions"])
        scheduled = PREDICTION_SECONDS[index]
        if elapsed < scheduled:
            break
        prediction = record["signal"]["prediction"]
        valid = elapsed - scheduled <= PREDICTION_GRACE_SECONDS and prediction in ("YES", "NO")
        snapshot = {
            "number": index + 1, "scheduled_minute": PREDICTION_MINUTES[index],
            "prediction": prediction, "status": "captured" if valid else "missed",
            "observed_elapsed_seconds": elapsed if valid else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "ask": None, "bid": None, "confidence": None,
        }
        if valid:
            ask, bid = quotes(current, prediction)
            confidence = live_confidence(ask)
            snapshot.update(ask=str(ask), bid=str(bid), confidence=confidence)
            record["signal"]["live_confidence"] = confidence
        record["predictions"].append(snapshot)
        write_log("PREDICTION_UPDATE" if valid else "PREDICTION_MISSED", ticker,
                  prediction=prediction, details=json.dumps(snapshot))
        changed = True
    if changed and len(record["predictions"]) == len(PREDICTION_SECONDS):
        complete = all(p.get("ask") is not None for p in record["predictions"])
        average = average_prediction_confidence(record["predictions"]) if complete else None
        record["final_confidence"] = live_confidence(average) if average is not None else None
        write_log("PREDICTION_FINAL", ticker, confidence=record["final_confidence"] or "INCOMPLETE")
    return changed

def cycle(state):
    reconcile_entries(state)
    if EXIT_MONITOR is None:
        write_log("ENTRY_WAIT_TAKE_PROFIT", details="Start the paired exit monitor before the entry loop")
        return
    now = datetime.now(timezone.utc); market, started, closed = active_market(now)
    if not market: return
    ticker = market["ticker"]; elapsed = (now - started).total_seconds()
    if ticker in state.get("mm", {}).get("markets", {}):
        write_log("STRATEGY_SWITCH_WAIT", ticker, details="Archived strategy owns this market; wait for the next contract")
        return
    record = state["markets"].setdefault(ticker, {"buys": 0, "last_buy": 0, "signal": None, "predictions": [], "orders": [], "spot_entry_attempted": False, "final_entry_attempted": False, "dual_limit_attempted": False, "dual_limit_orders": [], "historical_strike_orders": [], "historical_triggered_strikes": [], "historical_take_profit_orders": []})
    if "predictions" not in record: record["predictions"] = []
    if "spot_entry_attempted" not in record: record["spot_entry_attempted"] = False
    if "opening_bias_attempted" not in record: record["opening_bias_attempted"] = False
    if "final_entry_attempted" not in record: record["final_entry_attempted"] = False
    if "dual_limit_attempted" not in record: record["dual_limit_attempted"] = False
    if "dual_limit_orders" not in record: record["dual_limit_orders"] = []
    if "historical_strike_orders" not in record: record["historical_strike_orders"] = []
    if "historical_triggered_strikes" not in record: record["historical_triggered_strikes"] = []
    if "historical_take_profit_orders" not in record: record["historical_take_profit_orders"] = []
    initialize_budget(record)
    record["entry_cancel_at"] = cancellation_deadline(closed)
    record["close_timestamp"] = closed.timestamp()
    save_state(state)
    reconcile_entries(state)
    if EXIT_MONITOR is not None and not EXIT_MONITOR.healthy:
        write_log("ENTRY_WAIT_TAKE_PROFIT", ticker, details="Exit monitor warming up or recovering")
        return
    if record["signal"] is None:
        signal = strike_ruler(prior_three(started) + [Decimal(str(market["floor_strike"]))], ABS_GAP_AVG)
        record["signal"] = {"prediction": signal.prediction, "base_confidence": signal.confidence, "moves": [str(x) for x in signal.moves], "flipped": signal.flipped}
        write_log("BASE_SIGNAL", ticker, prediction=signal.prediction, confidence=signal.confidence, details=json.dumps(record["signal"])); save_state(state)
    if HISTORICAL_STRIKE_ENABLED and "historical_strikes" not in record:
        try:
            record["historical_strikes"] = [str(value) for value in prior_strikes(started)]
            write_log("HISTORICAL_STRIKES", ticker, details=json.dumps(record["historical_strikes"]))
            save_state(state)
        except Exception as error:
            if not record.get("historical_strikes_error_logged"):
                record["historical_strikes_error_logged"] = True
                write_log("HISTORICAL_STRIKES_UNAVAILABLE", ticker, details=repr(error))
                save_state(state)
    signal = record["signal"]; current = client.market(ticker)
    elapsed = time.time() - started.timestamp()
    # One bias-selected opening order: never quote both complementary outcomes.
    if OPENING_BIAS_ENABLED and 0 <= elapsed < OPENING_WINDOW and not record["opening_bias_attempted"] and signal["prediction"] in ("YES", "NO"):
        record["opening_bias_attempted"] = True
        save_state(state)  # Persist before POST so a lost acknowledgement cannot duplicate it.
        price, target = next(iter(OPENING_BIAS_PAIR.items()))
        result, quantity = funded_entry(record, state, ticker, signal["prediction"], price, closed, "opening_bias",
            submit_before=started.timestamp() + OPENING_WINDOW, cancel_at=started.timestamp() + OPENING_WINDOW)
        if result.get("order_id"):
            record["orders"].append(result["order_id"])
        write_log("OPENING_BIAS_LIMIT", ticker, prediction=signal["prediction"], price=str(price), quantity=str(quantity),
                  details=json.dumps({"exit_target": str(target), "entry_cutoff": started.timestamp() + OPENING_WINDOW, "order": result}))
        save_state(state)
    if update_prediction(record, ticker, current, elapsed): save_state(state)
    reconcile_entries(state)
    # Only the independent paired monitor owns exits. Never fall back to a
    # single-price exit path when both entry tiers can hold inventory.
    can_buy = START <= elapsed < END and signal["prediction"] in ("YES", "NO") and signal.get("base_confidence") in ("HIGH", "MODERATE") and any(p.get("ask") is not None for p in record["predictions"]) and record["buys"] < MAX_BUYS and time.time() - record["last_buy"] >= INTERVAL
    if can_buy:
        ask, _ = quotes(current, signal["prediction"])
        counted = False
        for price, result, quantity in paired_entries(record, state, ticker, signal["prediction"], closed, "regular"):
            if result.get("order_id"):
                if not counted:
                    record["buys"] += 1; record["last_buy"] = time.time()
                    counted = True
                record["orders"].append(result["order_id"])
                write_log("BUY_LIMIT", ticker, prediction=signal["prediction"], confidence=signal.get("live_confidence", ""), price=str(price), quantity=str(quantity), details=f"paired purchase {record['buys']} of {MAX_BUYS}")
                save_state(state)
    if DUAL_LIMIT_BUYS_ENABLED and START <= elapsed < END and not record["dual_limit_attempted"]:
        record["dual_limit_attempted"] = True
        save_state(state)
        if place_dual_limit_buys(record, ticker, closed, state=state): save_state(state)
    if HISTORICAL_STRIKE_ENABLED and START <= elapsed < END and record.get("historical_strikes"):
        try:
            reference_spot = client.btc_reference_price()
            if place_historical_strike_entries(record, ticker, reference_spot, closed, state=state): save_state(state)
        except Exception as error:
            if not record.get("historical_spot_error_logged"):
                record["historical_spot_error_logged"] = True
                write_log("HISTORICAL_SPOT_UNAVAILABLE", ticker, details=repr(error))
                save_state(state)
    if 0 <= elapsed < SPOT_ENTRY_WINDOW and not record["spot_entry_attempted"]:
        spot = client.btc_reference_price(); strike = Decimal(str(current["floor_strike"]))
        if spot_is_above_strike(spot, strike, SPOT_ENTRY_THRESHOLD):
            ask, _ = quotes(current, "YES")
            if Decimal("0") < ask <= Decimal("1"):
                record["spot_entry_attempted"] = True; save_state(state)
                for price, result, quantity in paired_entries(record, state, ticker, "YES", closed, "spot", submit_before=started.timestamp() + SPOT_ENTRY_WINDOW):
                    if result.get("order_id"): record["orders"].append(result["order_id"])
                    save_state(state)
                    details = {"spot": str(spot), "strike": str(strike), "distance_above_strike": str(spot - strike), "order": result}
                    write_log("SPOT_TRIGGER_BUY", ticker, prediction="YES", confidence=live_confidence(ask), price=str(price), quantity=str(quantity), details=json.dumps(details))

def check():
    balance = client.balance(); markets = client.markets(series_ticker="KXBTC15M", status="open", limit=1)
    print(json.dumps({"authenticated": True, "balance_dollars": balance.get("balance_dollars"), "KXBTC15M_visible": bool(markets)}, indent=2))

def main():
    global EXIT_MONITOR
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    version = Path(__file__).with_name("VERSION").read_text().strip()
    print(f"Strike Ruler bot v{version}; execution={EXECUTION_STRATEGY}", flush=True)
    print(f"Entry cutoff={END}s; cancel cutoff={CANCEL_AFTER}s; market budget=${MARKET_BUDGET}; entry/exit pairs={[(str(p * 100), str(t * 100)) for p, t in ENTRY_EXIT_PAIRS.items()]} cents", flush=True)
    ignored = ("TAKE_PROFIT_CENTS", "TAKE_PROFIT_PERCENT", "STOP_EXIT_CENTS",
               "ENTRY_MIN_CENTS", "ENTRY_MAX_CENTS", "ENTRY_PRICE_CENTS", "EXIT_PRICE_CENTS",
               "FINAL_ENTRY_START_MINUTE", "FINAL_ENTRY_END_MINUTE", "FINAL_CONFIDENCE_MIN_PERCENT")
    for name in ignored:
        if name in os.environ:
            print(f"CONFIG_IGNORED: {name}; paired prices apply and no stop-loss is active", flush=True)
    if seconds_from_minutes(os.getenv("ENTRY_END_MINUTE", "5")) > END:
        print("CONFIG_CAPPED: entry cutoff is five minutes", flush=True)
    if os.getenv("PREDICTION_UPDATE_MINUTES", "2,4,6") != "2,4,6":
        print("CONFIG_IGNORED: prediction schedule is fixed at 2,4,6 minutes", flush=True)
    if args.check: check(); return
    if not ENABLED:
        print("Checking Kalshi production credentials (read-only)...", flush=True)
        check()
        print("LOCKED: production service is online; live order routing is disabled", flush=True)
        while True: time.sleep(3600)
    # Railway uses Linux. Hold the volume lock for this process's lifetime.
    import fcntl
    import signal
    validate_storage()
    with STATE.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another bot already owns this state volume")
        state = load_state()
        # Separate client and receipt file: signal/CF timeouts cannot block
        # exits, and this worker never writes the entry budget/strategy state.
        exit_client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""),
            os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""), timeout=5)
        EXIT_MONITOR = TakeProfitMonitor(exit_client, load_state,
            STATE.with_name(STATE.stem + "_take_profit.json"), pairs=ALL_ENTRY_EXIT_PAIRS,
            poll=float(os.getenv("EXIT_POLL_SECONDS", "1")))
        EXIT_MONITOR.start()
        print(f"TP_MONITOR_STARTED pairs={[(str(p * 100), str(t * 100)) for p, t in ALL_ENTRY_EXIT_PAIRS.items()]}; independent reduce-only IOC exits; resting bracket unavailable", flush=True)
        def stop(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                try:
                    cycle(state)
                except Exception as error:
                    write_log("ERROR", details=repr(error))
                time.sleep(int(os.getenv("POLL_SECONDS", "5")))
        except KeyboardInterrupt:
            save_state(state)
        finally:
            if EXIT_MONITOR is not None:
                EXIT_MONITOR.stop()

if __name__ == "__main__": main()

