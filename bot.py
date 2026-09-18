import argparse, csv, json, os, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv
from kalshi import KalshiClient
from strategy import strike_ruler, quantity_for_budget, live_confidence, average_open_price, average_prediction_confidence, spot_is_above_strike, seconds_from_minutes, gross_take_profit_target, fixed_take_profit_target

load_dotenv()
if os.getenv("MODE", "live").lower() != "live" or os.getenv("KALSHI_ENV", "production").lower() != "production":
    raise SystemExit("This package supports live Kalshi production only")

ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
ENTRY_MIN = Decimal(os.getenv("ENTRY_MIN_CENTS", "10")) / 100
ENTRY_MAX = Decimal(os.getenv("ENTRY_MAX_CENTS", "47")) / 100
MODERATE_ENTRY_MAX = Decimal(os.getenv("MODERATE_ENTRY_MAX_CENTS", "30")) / 100
BUDGET = Decimal(os.getenv("ENTRY_BUDGET_DOLLARS", "0.77"))
MAX_BUYS = int(os.getenv("MAX_PURCHASES_PER_MARKET", "7"))
INTERVAL = int(os.getenv("ENTRY_INTERVAL_SECONDS", "7"))
START = seconds_from_minutes(os.getenv("ENTRY_START_MINUTE", "2"))
END = seconds_from_minutes(os.getenv("ENTRY_END_MINUTE", "6"))
PREDICTION_MINUTES = tuple(int(x.strip()) for x in os.getenv("PREDICTION_UPDATE_MINUTES", "2,4,6").split(",") if x.strip())
PREDICTION_SECONDS = tuple(minute * 60 for minute in PREDICTION_MINUTES)
FINAL_START = seconds_from_minutes(os.getenv("FINAL_ENTRY_START_MINUTE", "12"))
FINAL_END = seconds_from_minutes(os.getenv("FINAL_ENTRY_END_MINUTE", "15"))
FINAL_CONFIDENCE_MIN = Decimal(os.getenv("FINAL_CONFIDENCE_MIN_PERCENT", "65")) / 100
SPOT_ENTRY_WINDOW = seconds_from_minutes(os.getenv("SPOT_ENTRY_WINDOW_MINUTES", "2"))
SPOT_ENTRY_THRESHOLD = Decimal(os.getenv("SPOT_ENTRY_THRESHOLD_DOLLARS", "80"))
TAKE_PROFIT_PERCENT = Decimal(os.getenv("TAKE_PROFIT_PERCENT", "15"))
TAKE_PROFIT_RETRY_SECONDS = int(os.getenv("TAKE_PROFIT_RETRY_SECONDS", "60"))
DUAL_LIMIT_BUYS_ENABLED = os.getenv("DUAL_LIMIT_BUYS_ENABLED", "true").lower() == "true"
DUAL_LIMIT_PRICE = Decimal(os.getenv("DUAL_LIMIT_PRICE_CENTS", "25")) / 100
DUAL_LIMIT_TTL_SECONDS = int(os.getenv("DUAL_LIMIT_TTL_SECONDS", "300"))
HISTORICAL_STRIKE_ENABLED = os.getenv("HISTORICAL_STRIKE_ENABLED", "true").lower() == "true"
HISTORICAL_STRIKE_COUNT = int(os.getenv("HISTORICAL_STRIKE_COUNT", "3"))
HISTORICAL_STRIKE_TOUCH_DOLLARS = Decimal(os.getenv("HISTORICAL_STRIKE_TOUCH_DOLLARS", "25"))
HISTORICAL_STRIKE_ENTRY_PRICE = Decimal(os.getenv("HISTORICAL_STRIKE_ENTRY_CENTS", "25")) / 100
HISTORICAL_STRIKE_PROFIT_CENTS = Decimal(os.getenv("HISTORICAL_STRIKE_PROFIT_CENTS", "10"))
HISTORICAL_STRIKE_TTL_SECONDS = int(os.getenv("HISTORICAL_STRIKE_TTL_SECONDS", "300"))
ABS_GAP_AVG = Decimal(os.getenv("ABSOLUTE_GAP_AVERAGE", "59.58"))
STATE = Path(os.getenv("STATE_PATH", "/data/state.json"))
LOG = Path(os.getenv("LOG_PATH", "/data/trades.csv"))
client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""), os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""))

def parse_time(value): return datetime.fromisoformat(value.replace("Z", "+00:00"))
def load_state(): return json.loads(STATE.read_text()) if STATE.exists() else {"markets": {}}
def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp"); temp.write_text(json.dumps(state, indent=2)); temp.replace(STATE)
def write_log(event, ticker="", **values):
    LOG.parent.mkdir(parents=True, exist_ok=True); exists = LOG.exists()
    fields = ["time_utc", "ticker", "event", "prediction", "confidence", "price", "quantity", "details"]
    with LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists: writer.writeheader()
        row = {field: "" for field in fields}; row.update(time_utc=datetime.now(timezone.utc).isoformat(), ticker=ticker, event=event); row.update(values); writer.writerow(row)

def active_market(now):
    for market in client.markets(series_ticker="KXBTC15M", status="open", limit=100):
        closed = parse_time(market["close_time"])
        started = closed - timedelta(minutes=15)
        if started <= now < closed: return market, started, closed
    return None, None, None

def prior_three(started):
    found = []
    for market in client.markets(series_ticker="KXBTC15M", status="settled", limit=100):
        closed, value = parse_time(market["close_time"]), market.get("expiration_value")
        if closed <= started and value not in (None, ""): found.append((closed, Decimal(str(value))))
    found.sort()
    if len(found) < 3: raise RuntimeError("DATA UNAVAILABLE: fewer than 3 finalized KXBTC15M settlements")
    return [value for _, value in found[-3:]]

def prior_strikes(started, count=HISTORICAL_STRIKE_COUNT):
    found = []
    for market in client.markets(series_ticker="KXBTC15M", status="settled", limit=100):
        closed = parse_time(market["close_time"])
        strike = market.get("floor_strike")
        if closed <= started and strike not in (None, ""):
            found.append((closed, Decimal(str(strike))))
    found.sort()
    if len(found) < count:
        raise RuntimeError(f"DATA UNAVAILABLE: fewer than {count} finalized KXBTC15M strikes")
    return [value for _, value in found[-count:]]

def quotes(market, prediction):
    if prediction == "YES": return Decimal(market["yes_ask_dollars"]), Decimal(market["yes_bid_dollars"])
    return Decimal(market["no_ask_dollars"]), Decimal(market["no_bid_dollars"])

def position(ticker):
    for item in client.positions(ticker):
        if item.get("ticker") == ticker: return Decimal(str(item.get("position_fp", "0")))
    return Decimal("0")

def entry_price_allowed(base_confidence, price):
    price = Decimal(str(price))
    if base_confidence == "HIGH":
        return ENTRY_MIN <= price <= ENTRY_MAX
    if base_confidence == "MODERATE":
        return ENTRY_MIN <= price <= MODERATE_ENTRY_MAX
    return False

def manage_exit(record, ticker, market, signal, closed, reserved_quantity=Decimal("0"), excluded_order_ids=None):
    held = position(ticker)
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    take_profit_order_id = record.get("take_profit_order_id")
    if held == 0:
        if take_profit_order_id in resting:
            client.cancel(take_profit_order_id)
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
            client.cancel(take_profit_order_id)
            write_log("CANCEL_TAKE_PROFIT", ticker, prediction=side, details=take_profit_order_id)
        changed = any(record.pop(key, None) is not None for key in (
            "take_profit_order_id", "take_profit_side", "take_profit_quantity", "take_profit_target",
            "take_profit_rejected_side", "take_profit_rejected_quantity",
            "take_profit_rejected_target", "take_profit_retry_after",
        ))
        return changed
    excluded_order_ids = set(excluded_order_ids or ())
    fills = [item for item in client.fills(ticker) if item.get("order_id") not in excluded_order_ids]
    average_entry = average_open_price(fills, side)
    if average_entry is None:
        write_log("EXIT_BASIS_UNAVAILABLE", ticker, prediction=side, price=str(bid), quantity=str(abs(held)))
        return False
    target = gross_take_profit_target(average_entry, TAKE_PROFIT_PERCENT, market.get("price_ranges"))
    current_matches = (
        take_profit_order_id in resting
        and record.get("take_profit_side") == side
        and Decimal(str(record.get("take_profit_quantity", "0"))) == quantity
        and Decimal(str(record.get("take_profit_target", "-1"))) == target
    )
    if current_matches:
        return False
    rejected_matches = (
        record.get("take_profit_rejected_side") == side
        and Decimal(str(record.get("take_profit_rejected_quantity", "0"))) == quantity
        and Decimal(str(record.get("take_profit_rejected_target", "-1"))) == target
    )
    if rejected_matches and time.time() < float(record.get("take_profit_retry_after", 0)):
        return False
    if take_profit_order_id in resting:
        client.cancel(take_profit_order_id)
        write_log("CANCEL_TAKE_PROFIT", ticker, prediction=side, details=take_profit_order_id)
    try:
        result = client.place_take_profit(ticker, held, target, closed.timestamp())
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
    record["take_profit_side"] = side
    record["take_profit_quantity"] = str(quantity)
    record["take_profit_target"] = str(target)
    for key in (
        "take_profit_rejected_side", "take_profit_rejected_quantity",
        "take_profit_rejected_target", "take_profit_retry_after",
    ):
        record.pop(key, None)
    details = {"average_entry": str(average_entry), "target": str(target), "take_profit_percent": str(TAKE_PROFIT_PERCENT), "order": result}
    write_log("TAKE_PROFIT_RESTING", ticker, prediction=side, confidence=signal.get("live_confidence", ""), price=str(target), quantity=str(quantity), details=json.dumps(details))
    return True

def cancel_entries(record, ticker):
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    for order_id in list(record["orders"]):
        if order_id in resting: client.cancel(order_id); write_log("CANCEL_ENTRY", ticker, details=order_id)
        record["orders"].remove(order_id)

def place_dual_limit_buys(record, ticker, closed, now_timestamp=None):
    """Post one fixed-price YES bid and one fixed-price NO bid for five minutes."""
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    cancel_at = min(now_timestamp + DUAL_LIMIT_TTL_SECONDS, closed.timestamp())
    quantity = quantity_for_budget(DUAL_LIMIT_PRICE, BUDGET)
    record["dual_limit_cancel_at"] = cancel_at
    record.setdefault("dual_limit_orders", [])
    changed = True
    for side in ("YES", "NO"):
        try:
            result = client.place_entry(ticker, side, quantity, DUAL_LIMIT_PRICE, cancel_at)
        except Exception as error:
            write_log(
                "DUAL_LIMIT_REJECTED", ticker, prediction=side, price=str(DUAL_LIMIT_PRICE),
                quantity=str(quantity), details=repr(error),
            )
            continue
        order_id = result.get("order_id")
        if order_id:
            record["dual_limit_orders"].append(order_id)
        details = {"cancel_at": cancel_at, "order": result}
        write_log(
            "DUAL_LIMIT_RESTING", ticker, prediction=side, price=str(DUAL_LIMIT_PRICE),
            quantity=str(quantity), details=json.dumps(details),
        )
    return changed

def cancel_expired_dual_limits(record, ticker, now_timestamp=None):
    order_ids = list(record.get("dual_limit_orders", []))
    if not order_ids:
        return False
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    if now_timestamp < float(record.get("dual_limit_cancel_at", 0)):
        return False
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    for order_id in order_ids:
        if order_id in resting:
            client.cancel(order_id)
            write_log("CANCEL_DUAL_LIMIT", ticker, details=order_id)
    record["dual_limit_orders"] = []
    record["dual_limit_cancelled"] = True
    return True

def strike_reaction_side(reference_spot, strike):
    reference_spot = Decimal(str(reference_spot)); strike = Decimal(str(strike))
    if reference_spot < strike:
        return "NO"
    if reference_spot > strike:
        return "YES"
    return None

def place_historical_strike_entries(record, ticker, spot, closed, now_timestamp=None):
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
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
        cancel_at = min(now_timestamp + HISTORICAL_STRIKE_TTL_SECONDS, closed.timestamp())
        quantity = quantity_for_budget(HISTORICAL_STRIKE_ENTRY_PRICE, BUDGET)
        order_record = {
            "strike": strike_key, "side": side, "quantity": str(quantity),
            "entry_price": str(HISTORICAL_STRIKE_ENTRY_PRICE), "cancel_at": cancel_at,
        }
        try:
            result = client.place_entry(
                ticker, side, quantity, HISTORICAL_STRIKE_ENTRY_PRICE, cancel_at,
            )
        except Exception as error:
            order_record["rejected"] = repr(error)
            record["historical_strike_orders"].append(order_record)
            write_log(
                "HISTORICAL_STRIKE_REJECTED", ticker, prediction=side,
                price=str(HISTORICAL_STRIKE_ENTRY_PRICE), quantity=str(quantity),
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
            price=str(HISTORICAL_STRIKE_ENTRY_PRICE), quantity=str(quantity),
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
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    for item in due:
        order_id = item["order_id"]
        if order_id in resting:
            client.cancel(order_id)
            write_log("CANCEL_HISTORICAL_STRIKE", ticker, prediction=item.get("side", ""), details=order_id)
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
            client.cancel(active_id)
            write_log("CANCEL_HISTORICAL_TAKE_PROFIT", ticker, details=active_id)
        changed = any(record.pop(key, None) is not None for key in (
            "historical_take_profit_order_id", "historical_take_profit_side",
            "historical_take_profit_quantity", "historical_take_profit_target",
        ))
        return changed
    side = "YES" if held > 0 else "NO"
    target = fixed_take_profit_target(
        average_entry or HISTORICAL_STRIKE_ENTRY_PRICE, HISTORICAL_STRIKE_PROFIT_CENTS,
    )
    current_matches = (
        active_id in resting
        and record.get("historical_take_profit_side") == side
        and Decimal(str(record.get("historical_take_profit_quantity", "0"))) == reserved_quantity
        and Decimal(str(record.get("historical_take_profit_target", "-1"))) == target
    )
    if current_matches:
        return False
    if active_id in resting:
        client.cancel(active_id)
        write_log("CANCEL_HISTORICAL_TAKE_PROFIT", ticker, prediction=side, details=active_id)
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
        "HISTORICAL_TAKE_PROFIT_RESTING", ticker, prediction=side,
        price=str(target), quantity=str(reserved_quantity), details=json.dumps(result),
    )
    return True

def update_prediction(record, ticker, current, elapsed):
    completed = len(record["predictions"])
    if completed >= len(PREDICTION_SECONDS) or elapsed < PREDICTION_SECONDS[completed]:
        return False
    number = completed + 1
    prediction = record["signal"]["prediction"]
    ask, bid = quotes(current, prediction)
    confidence = live_confidence(ask)
    snapshot = {
        "number": number,
        "scheduled_minute": PREDICTION_MINUTES[completed],
        "prediction": prediction,
        "confidence": confidence,
        "ask": str(ask),
        "bid": str(bid),
    }
    record["predictions"].append(snapshot)
    record["signal"]["live_confidence"] = confidence
    write_log("PREDICTION_UPDATE", ticker, prediction=prediction, confidence=confidence, price=str(ask), details=json.dumps(snapshot))
    return True

def cycle(state):
    now = datetime.now(timezone.utc); market, started, closed = active_market(now)
    if not market: return
    ticker = market["ticker"]; elapsed = (now - started).total_seconds()
    record = state["markets"].setdefault(ticker, {"buys": 0, "last_buy": 0, "signal": None, "predictions": [], "orders": [], "spot_entry_attempted": False, "final_entry_attempted": False, "dual_limit_attempted": False, "dual_limit_orders": [], "historical_strike_orders": [], "historical_triggered_strikes": [], "historical_take_profit_orders": []})
    if "predictions" not in record: record["predictions"] = []
    if "spot_entry_attempted" not in record: record["spot_entry_attempted"] = False
    if "final_entry_attempted" not in record: record["final_entry_attempted"] = False
    if "dual_limit_attempted" not in record: record["dual_limit_attempted"] = False
    if "dual_limit_orders" not in record: record["dual_limit_orders"] = []
    if "historical_strike_orders" not in record: record["historical_strike_orders"] = []
    if "historical_triggered_strikes" not in record: record["historical_triggered_strikes"] = []
    if "historical_take_profit_orders" not in record: record["historical_take_profit_orders"] = []
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
    if DUAL_LIMIT_BUYS_ENABLED and START <= elapsed < END and not record["dual_limit_attempted"]:
        record["dual_limit_attempted"] = True
        save_state(state)
        if place_dual_limit_buys(record, ticker, closed): save_state(state)
    if HISTORICAL_STRIKE_ENABLED and START <= elapsed < END and record.get("historical_strikes"):
        try:
            reference_spot = client.btc_reference_price()
            if place_historical_strike_entries(record, ticker, reference_spot, closed): save_state(state)
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
                quantity = quantity_for_budget(ask, BUDGET)
                record["spot_entry_attempted"] = True; save_state(state)
                result = client.place_entry(ticker, "YES", quantity, ask, started.timestamp() + SPOT_ENTRY_WINDOW)
                details = {"spot": str(spot), "strike": str(strike), "distance_above_strike": str(spot - strike), "order": result}
                write_log("SPOT_TRIGGER_BUY", ticker, prediction="YES", confidence=live_confidence(ask), price=str(ask), quantity=str(quantity), details=json.dumps(details))
    if update_prediction(record, ticker, current, elapsed): save_state(state)
    if cancel_expired_dual_limits(record, ticker): save_state(state)
    if cancel_expired_historical_entries(record, ticker): save_state(state)
    held = position(ticker)
    historical_quantity, historical_order_ids, historical_average_entry = historical_inventory(record, ticker, held)
    if manage_exit(record, ticker, current, signal, closed, historical_quantity, historical_order_ids): save_state(state)
    if manage_historical_take_profit(record, ticker, held, historical_quantity, closed, historical_average_entry): save_state(state)
    if elapsed >= END and record["orders"]: cancel_entries(record, ticker); save_state(state)
    can_buy = START <= elapsed < END and signal["prediction"] in ("YES", "NO") and signal.get("base_confidence") in ("HIGH", "MODERATE") and record["predictions"] and record["buys"] < MAX_BUYS and time.time() - record["last_buy"] >= INTERVAL
    if can_buy:
        ask, _ = quotes(current, signal["prediction"])
        if entry_price_allowed(signal.get("base_confidence"), ask):
            quantity = quantity_for_budget(ask, BUDGET)
            result = client.place_entry(ticker, signal["prediction"], quantity, ask, started.timestamp() + END)
            if result.get("order_id"): record["orders"].append(result["order_id"])
            record["buys"] += 1; record["last_buy"] = time.time()
            write_log("BUY_LIMIT", ticker, prediction=signal["prediction"], confidence=signal.get("live_confidence", ""), price=str(ask), quantity=str(quantity), details=f"purchase {record['buys']} of {MAX_BUYS}"); save_state(state)
    average_confidence = average_prediction_confidence(record["predictions"])
    can_place_final = FINAL_START <= elapsed < FINAL_END and signal["prediction"] in ("YES", "NO") and len(record["predictions"]) == len(PREDICTION_SECONDS) and average_confidence is not None and average_confidence >= FINAL_CONFIDENCE_MIN and not record["final_entry_attempted"]
    if can_place_final:
        ask, _ = quotes(current, signal["prediction"])
        if Decimal("0") < ask <= Decimal("1"):
            quantity = quantity_for_budget(ask, BUDGET)
            record["final_entry_attempted"] = True; save_state(state)
            result = client.place_entry(ticker, signal["prediction"], quantity, ask, started.timestamp() + FINAL_END)
            details = {"average_confidence": live_confidence(average_confidence), "snapshots": len(record["predictions"]), "order": result}
            write_log("FINAL_BUY_LIMIT", ticker, prediction=signal["prediction"], confidence=live_confidence(average_confidence), price=str(ask), quantity=str(quantity), details=json.dumps(details))

def check():
    balance = client.balance(); markets = client.markets(series_ticker="KXBTC15M", status="open", limit=1)
    print(json.dumps({"authenticated": True, "balance_dollars": balance.get("balance_dollars"), "KXBTC15M_visible": bool(markets)}, indent=2))

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    print("Strike Ruler bot v0.8.0", flush=True)
    if args.check: check(); return
    if not ENABLED:
        print("Checking Kalshi production credentials (read-only)...", flush=True)
        check()
        print("LOCKED: production service is online; live order routing is disabled", flush=True)
        while True: time.sleep(3600)
    state = load_state()
    while True:
        try: cycle(state)
        except Exception as error: write_log("ERROR", details=repr(error))
        time.sleep(int(os.getenv("POLL_SECONDS", "7")))

if __name__ == "__main__": main()
