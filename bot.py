import argparse, csv, json, os, time, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv
from kalshi import KalshiClient, KalshiAPIError, terminal_ioc_receipt
from request_coordinator import RequestCoordinator, RequestDeferred
from entry_policy import initialize as initialize_budget, reserve as reserve_entry, market_budget, release_unsubmitted, entry_quantity, FEE_RESERVE, ENTRY_QUANTITY, EARLIER_ORDER_BUDGET, SETTLEMENT_BUDGET, SETTLEMENT_PRICE, SETTLEMENT_KIND, SETTLEMENT_WINDOW, settlement_price_allowed, settlement_entry_price_allowed, remaining_allowance, reconcile_reservation, attempt_committed
from take_profit import TakeProfitMonitor
from price_pairs import parse_pairs
from sale_recycling import confirmed_credits
from balance_diagnostics import log_api_cash, BalanceMonitor
from boruto import BUILD as SIGNAL_BUILD, build_signal
from strategy import strike_ruler, live_confidence, average_open_price, average_prediction_confidence, spot_is_above_strike, seconds_from_minutes

load_dotenv()
if os.getenv("MODE", "live").lower() != "live" or os.getenv("KALSHI_ENV", "production").lower() != "production":
    raise SystemExit("This package supports live Kalshi production only")

ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
EXECUTION_STRATEGY = os.getenv("EXECUTION_STRATEGY", "strike_ruler").lower()
if EXECUTION_STRATEGY != "strike_ruler":
    raise SystemExit("Only EXECUTION_STRATEGY=strike_ruler is supported")
ENTRY_EXIT_PAIRS = parse_pairs(os.getenv("ENTRY_EXIT_PAIRS_CENTS", "45:50"))
# Apply the active tiers even when Railway still has an older pair setting.
ENTRY_EXIT_PAIRS.update(parse_pairs("45:55,48:53,51:56,53:58,56:61,59:64,62:67,64:69,67:72,70:76,75:83"))
# A stale Railway pair must not introduce a late tier with a different target.
ENTRY_EXIT_PAIRS = {price: target for price, target in ENTRY_EXIT_PAIRS.items()
                    if price < Decimal("0.70") or price in (Decimal("0.70"), Decimal("0.75"))}
# Retired tiers must never be reintroduced by a stale environment variable.
ENTRY_EXIT_PAIRS.pop(Decimal("0.35"), None)
# The added 57c entry belongs only to its independent opening route.
ENTRY_EXIT_PAIRS.pop(Decimal("0.57"), None)
ENTRY_EXIT_PAIRS.pop(Decimal("0.38"), None)
ENTRY_EXIT_PAIRS.pop(Decimal("0.39"), None)
ENTRY_EXIT_PAIRS = dict(sorted(ENTRY_EXIT_PAIRS.items()))
# Retire the old entry even when an existing environment still lists it.
ENTRY_EXIT_PAIRS.pop(Decimal("0.32"), None)
if not ENTRY_EXIT_PAIRS:
    raise ValueError("Configure at least one entry pair other than retired 32 cents")
OPENING_BIAS_ENABLED = os.getenv("OPENING_BIAS_ENABLED", "true").lower() == "true"
OPENING_BIAS_PAIR = parse_pairs(os.getenv("OPENING_BIAS_PAIR_CENTS", "52:60"))
OPENING_WINDOW = seconds_from_minutes(os.getenv("OPENING_WINDOW_MINUTES", "2"))
OPENING_EXTRA_PAIR = parse_pairs("57:62")
OPENING_EXTRA_WINDOW = 120
OPENING_55_PAIR = parse_pairs("55:61")
OPENING_55_WINDOW = 120
OPENING_OPPOSITE_WINDOW = 120
FINAL_TWO_MINUTES_ONLY = True
MIN_STRIKE_DISTANCE_DOLLARS = Decimal("25")
DIRECTIONAL_ENTRY_POLICY = True
LATE_ENTRY_PAIRS = parse_pairs(os.getenv("LATE_ENTRY_PAIRS_CENTS", "73:81,85:92"))
LATE_ENTRY_PAIRS.update(parse_pairs("73:79,85:91"))
LATE_ENTRY_PAIRS = {price: target for price, target in LATE_ENTRY_PAIRS.items()
                    if price in (Decimal("0.73"), Decimal("0.85"))}
LATE_ENTRY_START = seconds_from_minutes(os.getenv("LATE_ENTRY_START_MINUTE", "11"))
LATE_ENTRY_END = seconds_from_minutes(os.getenv("LATE_ENTRY_END_MINUTE", "13"))
if not 0 <= LATE_ENTRY_START < LATE_ENTRY_END <= 900:
    raise SystemExit("Late entry window must satisfy 0 <= start < end <= 15 minutes")
# Preserve exits and reconciliation for inventory opened under retired tiers.
LEGACY_EXIT_PAIRS = parse_pairs("35:42,38:43,39:46")
NEW_ENTRY_EXIT_PAIRS = dict(sorted({**ENTRY_EXIT_PAIRS, **OPENING_BIAS_PAIR, **LATE_ENTRY_PAIRS, **OPENING_EXTRA_PAIR, **OPENING_55_PAIR}.items()))
NEW_ENTRY_EXIT_PAIRS.pop(Decimal("0.35"), None)
ALL_ENTRY_EXIT_PAIRS = dict(sorted({**LEGACY_EXIT_PAIRS, **NEW_ENTRY_EXIT_PAIRS}.items()))
NEW_ENTRY_EXIT_PAIRS[SETTLEMENT_PRICE] = Decimal("1")
ALL_ENTRY_EXIT_PAIRS[SETTLEMENT_PRICE] = Decimal("1")  # Hold-to-settlement inventory bucket.
# Compatibility values for the retired synchronous single-tier helpers only.
ENTRY_PRICE, EXIT_PRICE = Decimal("0.32"), Decimal("0.39")
MIN_ENTRY_PRICE = Decimal("0.45")
MID_PRICE_ENTRY_FLOOR = Decimal("0.60")
MID_PRICE_ENTRY_START = 300
EARLY_ENTRY_PRICE_CEILING = Decimal("0.70")
HIGH_PRICE_ENTRY_START = 480
SIX_MINUTE_ENTRY_PRICE = Decimal("0.75")
SIX_MINUTE_ENTRY_START = 360
LOW_PRICE_ENTRY_END = 360
BLOCKED_BUY_MIN_PRICE = Decimal("0.70")
BLOCKED_BUY_MAX_PRICE = Decimal("0.85")
BLOCKED_BUY_START = 360
BLOCKED_BUY_END = 780
ENTRY_EXECUTION_VERSION = 10
MARKET_BUDGET = market_budget()
PER_ORDER_PROFIT_DOLLARS = Decimal(os.getenv("PER_ORDER_PROFIT_DOLLARS", "0.20"))
if not PER_ORDER_PROFIT_DOLLARS.is_finite() or PER_ORDER_PROFIT_DOLLARS <= 0:
    raise ValueError("PER_ORDER_PROFIT_DOLLARS must be finite and positive")
REGULAR_ENTRY_END = 720  # Keep regular and resting scalp entries closed at minute 12.
CANCEL_AFTER = REGULAR_ENTRY_END
# Compatibility argument only: reserve_entry enforces the shared allocation.
BUDGET = Decimal("0.77")
MAX_OPEN_CONTRACTS = Decimal("8")
INITIAL_OPEN_CONTRACTS = Decimal("5")
MAX_AVERAGE_CONTRACTS = Decimal("3")
MAX_AVERAGE_DOLLARS = Decimal("2.50")
AVERAGE_DOWN_CUTOFF = 180
INTERVAL = int(os.getenv("ENTRY_INTERVAL_SECONDS", "7"))
ENTRY_START_DELAY = 60  # Wait for the first minute of each market.
START = ENTRY_START_DELAY
END = REGULAR_ENTRY_END  # Regular scalp entries keep their existing minute-12 cutoff.
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
REQUEST_COORDINATOR = RequestCoordinator()
client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""), os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""), coordinator=REQUEST_COORDINATOR)
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

def completed_values(started, field, count, last_offset=0):
    """Return exact finalized Kalshi values in chronological order.

    ``last_offset`` is measured in completed 15-minute periods before the
    target opening.  Bias lookbacks use 1 so they map to T-45/T-30/T-15 and
    never include the settlement printed at the target's T boundary.
    """
    expected = [
        started - timedelta(minutes=15 * offset)
        for offset in reversed(range(last_offset, last_offset + count))
    ]
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
    return completed_values(started, "expiration_value", 3, last_offset=1)

def prior_strikes(started, count=HISTORICAL_STRIKE_COUNT):
    return completed_values(started, "floor_strike", count)

def quotes(market, prediction):
    if prediction == "YES": return Decimal(market["yes_ask_dollars"]), Decimal(market["yes_bid_dollars"])
    return Decimal(market["no_ask_dollars"]), Decimal(market["no_bid_dollars"])

def strike_side(market, spot, minimum_distance=Decimal("0")):
    """Choose the live outcome from Bitcoin's price versus this market's strike."""
    strike, spot = Decimal(str(market["floor_strike"])), Decimal(str(spot))
    if not strike.is_finite() or strike <= 0 or not spot.is_finite() or spot <= 0:
        raise ValueError("Invalid live strike or BTC reference price")
    distance = Decimal(str(minimum_distance))
    if not distance.is_finite() or distance < 0:
        raise ValueError("Invalid strike distance")
    if distance == 0:
        return "YES" if spot > strike else "NO" if spot < strike else None
    return "YES" if spot - strike >= distance else "NO" if strike - spot >= distance else None

def position(ticker):
    for item in client.positions(ticker):
        if item.get("ticker") == ticker: return Decimal(str(item.get("position_fp", "0")))
    return Decimal("0")

def entry_price_allowed(base_confidence, price):
    return base_confidence in ("HIGH", "MODERATE", "LOW") and Decimal(str(price)) in ENTRY_EXIT_PAIRS

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
    terminal = {"canceled", "executed", "expired"}
    try:
        status = client.order(order_id, ticker).get("status")
        if status in terminal:
            write_log("ENTRY_ALREADY_TERMINAL", ticker, details=json.dumps({"order_id": order_id, "status": status}))
            return True
    except Exception:
        pass  # A failed read must not prevent a routed cancellation attempt.
    cancel_error = None
    try:
        result = client.cancel(order_id, ticker)
        if result.get("order_id") == order_id and "reduced_by" in result:
            write_log("CANCEL_ENTRY_CONFIRMED", ticker, details=order_id)
            return True
    except Exception as error:
        cancel_error = error
    # A 404, timeout, or incomplete response is not proof of cancellation.
    try:
        status = client.order(order_id, ticker).get("status")
        if status in terminal:
            write_log("ENTRY_ALREADY_TERMINAL", ticker, details=json.dumps({"order_id": order_id, "status": status}))
            return True
    except Exception as error:
        write_log("ENTRY_STATUS_RETRY", ticker, details=f"{order_id}: {error!r}")
    if cancel_error is not None:
        write_log("ENTRY_CANCEL_RETRY", ticker, details=f"{order_id}: {cancel_error!r}")
    return False


def cancel_entries(record, ticker):
    for order_id in list(record.get("orders", [])):
        if cancel_confirmed(order_id, ticker):
            record["orders"].remove(order_id)


def entry_deadline(closed):
    return closed.timestamp() - 900 + END


def cancellation_deadline(closed):
    return closed.timestamp() - 900 + CANCEL_AFTER


def previous_market_bias(state, started):
    """Reconstruct the immediately previous bias from official Kalshi data.

    State is accepted for upgrade compatibility but is deliberately not used:
    a missing/stale ledger must never choose the previous side.
    """
    del state
    previous_started = started - timedelta(minutes=15)
    previous_strike = completed_values(started, "floor_strike", 1)[0]
    signal = strike_ruler(prior_three(previous_started) + [previous_strike], ABS_GAP_AVG)
    return signal.prediction if signal.prediction in ("YES", "NO") else None


def uses_entry_bias(price, kind):
    # Under-70c routes expire at minute 6. Later high-odds entries retain their
    # live-strike selection, including the separate settlement transition.
    return kind != SETTLEMENT_KIND and Decimal(str(price)) < EARLY_ENTRY_PRICE_CEILING


def entry_side_source(closed, price=Decimal("0"), kind="regular"):
    if kind == "opening_55":
        return "price_only"
    if not DIRECTIONAL_ENTRY_POLICY:
        elapsed = time.time() - (closed.timestamp() - 900)
        if uses_entry_bias(price, kind) and elapsed < LOW_PRICE_ENTRY_END:
            return "opposite_strike" if 0 <= elapsed < OPENING_OPPOSITE_WINDOW else "boruto"
    return "live_strike"


def selected_entry_side(record, state, ticker, market, closed, source):
    if source == "price_only":
        return None, None, None
    if source == "boruto":
        bias = ensure_entry_bias(record, state, ticker, market, closed)["prediction"]
        return bias, None, bias
    live = strike_side(market, client.btc_reference_price(),
                       MIN_STRIKE_DISTANCE_DOLLARS if DIRECTIONAL_ENTRY_POLICY else Decimal("0"))
    selected = {"YES": "NO", "NO": "YES"}.get(live) if source == "opposite_strike" else live
    return selected, live, None


def ensure_entry_bias(record, state, ticker, market, closed):
    """Lock the no-skip Boruto signal once per market for minutes 2 through 6."""
    started = closed - timedelta(minutes=15)
    saved = record.get("entry_bias")
    if saved is not None:
        if (saved.get("build") != SIGNAL_BUILD or saved.get("ticker") != ticker
                or parse_time(saved["open_time"]) != started
                or Decimal(saved["strike"]) != Decimal(str(market["floor_strike"]))
                or saved.get("prediction") not in ("YES", "NO")):
            raise RuntimeError("DATA UNAVAILABLE: saved entry bias does not match this market")
        return saved
    target = {**market, "ticker": ticker, "open_time": started.isoformat(),
              "close_time": closed.isoformat()}
    history = client.markets(series_ticker="KXBTC15M", status="settled", limit=100)
    signal = build_signal(target, history, datetime.now(timezone.utc))
    signal["ticker"] = ticker
    record["entry_bias"] = signal
    try:
        save_state(state)
    except Exception:
        record.pop("entry_bias", None)
        raise
    write_log("ENTRY_BIAS_LOCKED", ticker, prediction=signal["prediction"],
              confidence=signal["base_confidence"], details=json.dumps(signal))
    return signal


def entry_decision(record, side, price, kind, live_side=None, bias_side=None, side_source="live_strike"):
    allowed = live_side in ("YES", "NO") and side == live_side
    if kind == "opening_55" and side_source == "price_only":
        allowed = side in ("YES", "NO")
        reason = "opening_55_price_only"
    elif kind == SETTLEMENT_KIND:
        allowed = side in ("YES", "NO") and side == live_side and Decimal(str(price)) == SETTLEMENT_PRICE
        reason = "live_strike_97_cent_limit"
        switch = record.get("settlement_switch", {})
        if switch.get("side") != side or switch.get("phase") != "ready":
            locked_side = record.get("trade_side")
            if locked_side in ("YES", "NO") and side != locked_side:
                allowed = False
                reason = "settlement_switch_not_confirmed"
    elif side_source == "opposite_strike":
        allowed = live_side in ("YES", "NO") and side == {"YES": "NO", "NO": "YES"}[live_side]
        reason = ("opposite_strike_" + kind if allowed else
                  "btc_at_strike" if live_side is None else "selected_side_not_opposite_strike")
    elif side_source == "boruto":
        allowed = bias_side in ("YES", "NO") and side == bias_side
        reason = ("boruto_bias_" + kind if allowed else
                  "bias_unavailable" if bias_side is None else "selected_side_opposes_bias")
    elif live_side is None:
        reason = "btc_within_25_dollars_of_strike"
    elif side != live_side:
        reason = "selected_side_opposes_live_strike"
    else:
        reason = "live_strike_" + kind
    if Decimal(str(price)) < MIN_ENTRY_PRICE:
        allowed = False
        reason = "entry_limit_below_45c"
    return allowed, {
        "selected_side": side,
        "live_strike_side": live_side,
        "bias_side": bias_side,
        "side_source": side_source,
        "entry_price": str(price),
        "entry_reason": reason,
        "decision": "ALLOW" if allowed else "SKIP",
    }


def tracked_entry_price_allowed(intent):
    if intent.get("kind") == SETTLEMENT_KIND:
        return (settlement_price_allowed(intent.get("price", "-1"))
                and intent.get("hold_to_settlement") is True
                and Decimal(str(intent.get("exit_target", "-1"))) == 1)
    return Decimal(str(intent.get("price", "-1"))) in ALL_ENTRY_EXIT_PAIRS


def committed_regular_orders(record):
    return sum(i.get("kind") == "regular" and attempt_committed(i)
               for i in record.get("entry_intents", []))


def pending_entry_contracts(record):
    # Unknown acknowledgements and resting orders can still fill. Count their
    # entire remaining quantity until reconciliation proves them terminal.
    return sum((Decimal(i["quantity"]) for i in record.get("entry_intents", [])
                if not i.get("entry_closed")), Decimal("0"))


def scalp_entry_limit(record, held, price, kind, closed):
    pending = pending_entry_contracts(record)
    room = MAX_OPEN_CONTRACTS - abs(held) - pending
    if kind == SETTLEMENT_KIND:
        return max(Decimal("0"), room), False, None
    if held == 0 and pending == 0:
        # A fully closed position starts a new episode. A sale during an open
        # position cannot reset the one-average-down allowance.
        record["scalp_episode"] = {"id": str(uuid.uuid4())}
    episode = record.get("scalp_episode")
    if not episode:
        # On upgrade, the bot cannot safely reconstruct whether an existing
        # position has already been averaged down. Wait until it is flat.
        return Decimal("0"), False, "untracked_open_position"
    averaging = held != 0
    if not averaging:
        return max(Decimal("0"), min(room, INITIAL_OPEN_CONTRACTS - pending)), False, None
    if time.time() >= closed.timestamp() - 900 + AVERAGE_DOWN_CUTOFF:
        return Decimal("0"), True, "average_down_window_closed"
    if any(i.get("scalp_episode") == episode["id"] and i.get("averaging_entry")
           and attempt_committed(i) for i in record.get("entry_intents", [])):
        return Decimal("0"), True, "average_down_already_used"
    affordable = (MAX_AVERAGE_DOLLARS / (Decimal(price) + FEE_RESERVE)).to_integral_value(rounding="ROUND_DOWN")
    return max(Decimal("0"), min(room, MAX_AVERAGE_CONTRACTS, affordable)), True, None


def blocked_buy_window(price, closed_timestamp, now_timestamp):
    """The 70–85c restriction overrides every route, including saved orders."""
    return (BLOCKED_BUY_MIN_PRICE <= Decimal(str(price)) <= BLOCKED_BUY_MAX_PRICE
            and BLOCKED_BUY_START <= now_timestamp - (closed_timestamp - 900) < BLOCKED_BUY_END)


def funded_entry(record, state, ticker, side, price, closed, kind, now_timestamp=None, submit_before=None, order_budget=None, cancel_at=None):
    def skip(reason, **details):
        write_log("ENTRY_SKIP", ticker, prediction=side, price=str(price), details=json.dumps({
            "reason": reason, "kind": kind,
            "elapsed_seconds": round(time.time() - (closed.timestamp() - 900), 3), **details}))
        return {}, Decimal("0")

    # Use the actual clock, not a caller-supplied timestamp or stale Railway
    # configuration. Opening and optional trigger routes share this minimum.
    if time.time() < closed.timestamp() - 900 + ENTRY_START_DELAY:
        return skip("market_not_started", minimum_elapsed_seconds=ENTRY_START_DELAY)
    if blocked_buy_window(price, closed.timestamp(), time.time()):
        return skip("70_85_cent_window_blocked")
    # Gate the submitted limit on every route. A 62c/67c limit can otherwise
    # execute at a 60c ask even when a lower tier would have waited.
    if Decimal(str(price)) >= MID_PRICE_ENTRY_FLOOR and time.time() < closed.timestamp() - 900 + MID_PRICE_ENTRY_START:
        write_log("ENTRY_FIVE_MINUTE_WAIT", ticker, prediction=side, price=str(price),
                  details="Buy limits of 60c or more open no earlier than 5:00")
        return skip("before_five_minutes")
    # Persisted intents are also the opening-tier attempt ledger, so a lost
    # acknowledgement or restart cannot duplicate the new 57c order.
    if Decimal(str(price)) in OPENING_EXTRA_PAIR and any(
        Decimal(str(i.get("price", "-1"))) == Decimal(str(price))
        and attempt_committed(i)
        for i in record.get("entry_intents", [])
    ):
        return skip("opening_attempt_already_committed")
    if kind in {"opening_bias", "late_bias", "opening_55"} and any(
        i.get("kind") == kind and Decimal(str(i.get("price", "-1"))) == Decimal(str(price))
        and attempt_committed(i) for i in record.get("entry_intents", [])
    ):
        return skip("route_attempt_already_committed")
    # One working 75c order per market. More can be placed after it fills,
    # subject to the regular interval and the unrecycled filled-spend cap.
    if Decimal(str(price)) == SIX_MINUTE_ENTRY_PRICE and any(
        not i.get("entry_closed") and Decimal(str(i.get("price", "-1"))) == Decimal(str(price))
        for i in record.get("entry_intents", [])
    ):
        return skip("resting_entry_already_pending")
    switch = record.get("settlement_switch")
    if switch:
        if kind != SETTLEMENT_KIND or switch.get("side") != side or switch.get("phase") != "ready":
            return skip("settlement_transition_pending")
        if EXIT_MONITOR is None or not EXIT_MONITOR.settlement_ready(ticker, side):
            return skip("settlement_exit_not_ready")
    side_source = entry_side_source(closed, price, kind)
    try:
        current_market = client.market(ticker)
        _, live_side, bias_side = selected_entry_side(record, state, ticker, current_market, closed, side_source)
    except Exception as error:
        write_log("ENTRY_SIDE_UNAVAILABLE", ticker, details=repr(error))
        return skip("bias_unavailable" if side_source == "boruto" else "live_side_unavailable")
    allowed, decision = entry_decision(record, side, price, kind, live_side, bias_side, side_source)
    write_log("ENTRY_DECISION", ticker, prediction=side, price=str(price), details=json.dumps(decision))
    if not allowed:
        return skip(decision["entry_reason"])
    if EXIT_MONITOR is not None and not EXIT_MONITOR.healthy:
        write_log("ENTRY_WAIT_TAKE_PROFIT", ticker, details="Independent exit monitor is not healthy")
        return skip("take_profit_unhealthy")
    if kind != SETTLEMENT_KIND and Decimal(str(price)) not in NEW_ENTRY_EXIT_PAIRS:
        raise ValueError("Entry price must match a configured fixed entry limit")
    if any(not i.get("entry_closed") and (i.get("entry_execution_version") != ENTRY_EXECUTION_VERSION
               or not tracked_entry_price_allowed(i))
           for i in record.get("entry_intents", [])):
        return skip("old_entry_reconciliation_pending")
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    policy_cutoff = closed.timestamp() - 900 + (LOW_PRICE_ENTRY_END
        if Decimal(str(price)) < EARLY_ENTRY_PRICE_CEILING else END)
    if kind == "opening_55":
        opening_cutoff = closed.timestamp() - 900 + OPENING_55_WINDOW
        if not closed.timestamp() - 900 + ENTRY_START_DELAY <= time.time() < opening_cutoff:
            return skip("outside_opening_55_window")
        policy_cutoff = min(policy_cutoff, opening_cutoff)
    if side_source == "opposite_strike":
        policy_cutoff = min(policy_cutoff, closed.timestamp() - 900 + OPENING_OPPOSITE_WINDOW)
    if Decimal(str(price)) in OPENING_EXTRA_PAIR:
        if time.time() < closed.timestamp() - 900:
            return skip("market_not_started")
        policy_cutoff = min(policy_cutoff, closed.timestamp() - 900 + OPENING_EXTRA_WINDOW)
    if kind == SETTLEMENT_KIND:
        if not closed.timestamp() - SETTLEMENT_WINDOW <= time.time() < closed.timestamp():
            return skip("outside_settlement_window")
        policy_cutoff = closed.timestamp()
    cutoff = min(policy_cutoff, float(submit_before) if submit_before is not None else policy_cutoff)
    # Expire any eligible pre-window order at 6:00. This also keeps a slow
    # quote/funding request from crossing the boundary before HTTP dispatch.
    if (BLOCKED_BUY_MIN_PRICE <= Decimal(str(price)) <= BLOCKED_BUY_MAX_PRICE
            and time.time() < closed.timestamp() - 900 + BLOCKED_BUY_START):
        cutoff = min(cutoff, closed.timestamp() - 900 + BLOCKED_BUY_START)
    if max(now_timestamp, time.time()) >= cutoff:
        return skip("entry_window_closed", cutoff=cutoff)
    # Enforce on every route, using the actual clock rather than a caller's
    # possibly stale timestamp. The explicit 75c tier opens at six minutes;
    # the other high-price limits still wait for eight minutes.
    high_price_start = SIX_MINUTE_ENTRY_START if Decimal(str(price)) == SIX_MINUTE_ENTRY_PRICE else HIGH_PRICE_ENTRY_START
    high_price_opens = closed.timestamp() - 900 + high_price_start
    if Decimal(str(price)) >= EARLY_ENTRY_PRICE_CEILING and time.time() < high_price_opens:
        write_log("ENTRY_EARLY_PRICE_WAIT", ticker, prediction=side, price=str(price),
                  details=json.dumps({"minimum_elapsed_seconds": high_price_start,
                                      "ceiling_exclusive": str(EARLY_ENTRY_PRICE_CEILING)}))
        return skip("high_price_window_not_open", minimum_elapsed_seconds=high_price_start)
    if kind != SETTLEMENT_KIND:
        try:
            held = settlement_position(ticker)
        except Exception as error:
            write_log("ENTRY_POSITION_UNAVAILABLE", ticker, details=type(error).__name__)
            return skip("position_unavailable")
        if (side == "YES" and held < 0) or (side == "NO" and held > 0):
            write_log("ENTRY_OPPOSITE_POSITION_WAIT", ticker, prediction=side, details=str(held))
            return skip("opposite_inventory", held=str(held))
        if any(not i.get("entry_closed") and i.get("side") != side
                for i in record.get("entry_intents", [])):
            return skip("opposite_entry_unresolved")
    else:
        try:
            held = settlement_position(ticker)
        except Exception:
            return skip("settlement_position_unavailable")
    account_held = held
    try:
        held = EXIT_MONITOR.bot_inventory(ticker, record, account_held)
    except Exception as error:
        return skip("bot_inventory_pending", account_held=str(account_held), detail=str(error))
    quantity_limit, averaging_entry, limit_reason = scalp_entry_limit(record, held, price, kind, closed)
    if limit_reason or quantity_limit < 1:
        return skip(limit_reason or "open_contract_limit", held=str(held), account_held=str(account_held),
                    pending=str(pending_entry_contracts(record)), maximum=str(MAX_OPEN_CONTRACTS))
    if time.time() < record.get("cash_retry_at", 0):
        return skip("cash_retry_delay", retry_at=record["cash_retry_at"])
    quantity = entry_quantity(price, kind, record, MARKET_BUDGET, max_quantity=quantity_limit)
    available_budget = remaining_allowance(record, MARKET_BUDGET, kind)
    if record.get("entry_budget_legacy") or quantity < 1 or quantity * (Decimal(price) + FEE_RESERVE) > available_budget:
        return skip("market_allowance_unavailable", remaining_dollars=str(available_budget), quantity=str(quantity))
    try:
        funding = client.market_cash(ticker)
        available = Decimal(funding["cash_dollars"])
        required = quantity * (Decimal(price) + FEE_RESERVE)
        if not available.is_finite():
            raise ValueError("Invalid market cash")
    except Exception as error:
        record["cash_retry_at"] = time.time() + 30
        save_state(state)
        write_log("ENTRY_CASH_UNAVAILABLE", ticker, details=json.dumps({"error_type": type(error).__name__}))
        return skip("cash_read_unavailable")
    if available < required:
        record["cash_retry_at"] = time.time() + 30
        save_state(state)
        write_log("ENTRY_WAIT_MARKET_CASH", ticker, price=str(price), details=json.dumps({
            "exchange_index": funding["exchange_index"], "cash_dollars": str(available),
            "required_dollars": str(required), "retry_seconds": 30,
            "reason": "Fund this market's exchange shard; aggregate cash is not spendable here",
        }))
        return skip("insufficient_market_cash", available_dollars=str(available), required_dollars=str(required))
    # Read the selected outcome immediately before reserving/submitting. Only
    # the explicit 75c and settlement limits may rest after submission.
    try:
        fresh_market = client.market(ticker)
        fresh_side, _, _ = selected_entry_side(record, state, ticker, fresh_market, closed, side_source)
        if side_source != "price_only" and fresh_side != side:
            return skip("entry_side_changed_before_post")
        ask, _ = quotes(fresh_market, side)
        if kind != SETTLEMENT_KIND and Decimal(str(price)) >= EARLY_ENTRY_PRICE_CEILING:
            other = "NO" if side == "YES" else "YES"
            other_ask, _ = quotes(fresh_market, other)
            if not other_ask.is_finite() or ask <= other_ask or ask < EARLY_ENTRY_PRICE_CEILING:
                write_log("ENTRY_HIGHER_SIDE_WAIT", ticker, prediction=side,
                          details=json.dumps({"selected_ask": str(ask), "other_ask": str(other_ask)}))
                return skip("not_higher_70_plus_side", ask=str(ask), other_ask=str(other_ask))
        minimum_ask = MIN_ENTRY_PRICE
        if not ask.is_finite() or not minimum_ask <= ask < Decimal("1"):
            write_log("ENTRY_PRICE_FLOOR_WAIT", ticker, prediction=side,
                      details=json.dumps({"ask": str(ask), "minimum": str(minimum_ask)}))
            return skip("ask_outside_entry_bounds", ask=str(ask))
        resting = kind == SETTLEMENT_KIND or Decimal(str(price)) == SIX_MINUTE_ENTRY_PRICE
        if kind == SETTLEMENT_KIND and not settlement_entry_price_allowed(ask):
            return skip("settlement_ask_below_97", ask=str(ask))
        if not resting and ask > Decimal(str(price)):
            return skip("ask_above_limit", ask=str(ask), limit=str(price))
        if Decimal(str(price)) == SIX_MINUTE_ENTRY_PRICE and ask < Decimal(str(price)):
            return skip("75_cent_trigger_not_reached", ask=str(ask))
        # The opening 57c rule retains its exact-quote trigger.
        if Decimal(str(price)) in OPENING_EXTRA_PAIR and ask != Decimal(str(price)):
            return skip("opening_57_quote_mismatch", ask=str(ask))
    except Exception as error:
        write_log("ENTRY_QUOTE_UNAVAILABLE", ticker, details=repr(error))
        return skip("quote_read_unavailable")
    if time.time() >= cutoff:
        return skip("deadline_reached_after_quote")
    if kind != SETTLEMENT_KIND:
        try:
            held = settlement_position(ticker)
        except Exception:
            return skip("final_position_unavailable")
        if (side == "YES" and held < 0) or (side == "NO" and held > 0):
            return skip("opposite_inventory_before_post", held=str(held))
    else:
        held = settlement_position(ticker)
    account_held = held
    try:
        held = EXIT_MONITOR.bot_inventory(ticker, record, account_held)
    except Exception as error:
        return skip("bot_inventory_pending_before_post", account_held=str(account_held), detail=str(error))
    if abs(held) + pending_entry_contracts(record) + quantity > MAX_OPEN_CONTRACTS:
        return skip("open_contract_limit_before_post", held=str(held), quantity=str(quantity))
    if switch:
        latest_account_held = settlement_position(ticker)
        if latest_account_held != account_held:
            return skip("position_changed_before_settlement")
        if (side == "YES" and latest_account_held < 0) or (side == "NO" and latest_account_held > 0):
            return skip("opposite_inventory_before_settlement", held=str(latest_account_held))
        if time.time() >= cutoff or not EXIT_MONITOR.settlement_ready(ticker, side):
            return skip("settlement_not_ready_before_post")
    if time.time() >= cutoff:
        return skip("deadline_reached_before_reservation")
    if blocked_buy_window(price, closed.timestamp(), time.time()):
        return skip("70_85_cent_window_blocked_before_post")
    if account_held != held:
        write_log("ENTRY_MANUAL_INVENTORY_EXCLUDED", ticker, prediction=side,
                  details=json.dumps({"account_held": str(account_held), "bot_held": str(held),
                                      "manual_held": str(account_held - held), "quantity": str(quantity)}))
    cancel_at = min(cutoff, cancellation_deadline(closed) if cancel_at is None else cancel_at)
    intent = reserve_entry(record, side, price, BUDGET if order_budget is None else order_budget,
                           MARKET_BUDGET, cancel_at, kind, max_quantity=quantity)
    if intent is None:
        return skip("reservation_unavailable", remaining_dollars=str(remaining_allowance(record, MARKET_BUDGET, kind)))
    intent["entry_execution_version"] = ENTRY_EXECUTION_VERSION
    if kind != SETTLEMENT_KIND:
        intent["scalp_episode"] = record["scalp_episode"]["id"]
        intent["averaging_entry"] = averaging_entry
    intent["side_source"] = decision["side_source"]
    if side_source == "boruto":
        intent["bias_build"] = SIGNAL_BUILD
    intent["exit_target"] = "1" if kind == SETTLEMENT_KIND else str(ALL_ENTRY_EXIT_PAIRS[Decimal(str(price))])
    if kind == "opening_55":
        intent["fixed_exit_target"] = True
    intent["resting_entry"] = resting
    if kind == SETTLEMENT_KIND:
        intent["hold_to_settlement"] = True
    save_state(state)  # Persist allowance and client ID before any exchange request.
    quantity = Decimal(intent["quantity"])
    try:
        result = client.place_entry(ticker, side, quantity, price, intent["cancel_at"],
                                    submit_before=cutoff, client_order_id=intent["client_id"],
                                    ioc=not resting)
    except RequestDeferred as error:
        # The gate rejected this locally before HTTP dispatch. Keep the audit
        # record, but do not spend allowance on an order that was never sent.
        release_unsubmitted(intent, "request_deferred")
        save_state(state)
        write_log("ENTRY_REQUEST_DEFERRED", ticker, details=json.dumps({
            "client_id": intent["client_id"], "retry_seconds": error.retry_after,
        }))
        return {}, Decimal("0")
    except KalshiAPIError as error:
        if error.status_code == 400 and error.code == "insufficient_balance":
            release_unsubmitted(intent, "insufficient_balance")
            record["cash_retry_at"] = time.time() + 30
            save_state(state)
            write_log("ENTRY_RESERVATION_RELEASED", ticker, price=str(price), details=json.dumps({
                "reason": intent["release_reason"], "released_dollars": intent["released_dollars"],
                "client_id": intent["client_id"], "kind": kind,
            }))
        elif error.status_code in {400, 401, 403, 404, 422, 429}:
            intent["entry_closed"] = True
        save_state(state)
        raise
    if result.get("order_id"):
        intent["order_id"] = result["order_id"]
        intent["placement_receipt"] = result
        if kind != SETTLEMENT_KIND:
            record["trade_side"] = side
    elif not result:
        intent["entry_closed"] = True  # Keep allowance without explicit rejection proof.
    save_state(state)
    if EXIT_MONITOR is not None:
        EXIT_MONITOR.wake()
    write_log("ENTRY_BUDGET", ticker, details=json.dumps({
        "cap": str(MARKET_BUDGET), "reserved": str(sum(Decimal(i["reserved_dollars"]) for i in record["entry_intents"])),
        "recycled": str(sum((Decimal(v) for v in record.get("recycled_exit_orders", {}).values()), Decimal(0))),
        "remaining_earlier": str(remaining_allowance(record, MARKET_BUDGET, "regular")),
        "kind": kind, "client_id": intent["client_id"], "cancel_at": intent["cancel_at"],
    }))
    return result, quantity


def paired_entries(record, state, ticker, side, closed, kind, now_timestamp=None, submit_before=None):
    """Submit up to five contracts per tier within its $2.80 allocation."""
    for price in ENTRY_EXIT_PAIRS:
        result, quantity = funded_entry(record, state, ticker, side, price, closed, kind,
            now_timestamp, submit_before, order_budget=BUDGET / len(ENTRY_EXIT_PAIRS))
        yield price, result, quantity


def paired_late_entries(record, state, ticker, side, closed, now_timestamp, submit_before, cancel_at):
    """Submit each late tier within its $2.80 and shared market allowances."""
    for price in LATE_ENTRY_PAIRS:
        result, quantity = funded_entry(
            record, state, ticker, side, price, closed, "late_bias", now_timestamp,
            submit_before=submit_before, order_budget=BUDGET / len(LATE_ENTRY_PAIRS),
            cancel_at=cancel_at,
        )
        yield price, result, quantity


def reconcile_entries(state, now_timestamp=None):
    """Run before market discovery/signals, including markets from earlier cycles."""
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    for ticker, record in state.get("markets", {}).items():
        pending = [i for i in record.get("entry_intents", []) if not i.get("entry_closed")]
        # Audit previously closed intents once during an active contract, too:
        # older releases kept the entire allowance even for zero-fill IOCs.
        close = record.get("close_timestamp", record.get("entry_cancel_at", 0))
        audit = [i for i in record.get("entry_intents", []) if i.get("order_id")
                 and not i.get("reservation_reconciled")
                 and (not i.get("entry_closed") or now_timestamp < close)]
        legacy_pending = record.get("orders") or record.get("dual_limit_orders") or any(
            i.get("order_id") and not i.get("entry_closed") for i in record.get("historical_strike_orders", []))
        if not pending and not legacy_pending and not audit:
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
            intents = record.get("entry_intents", [])
            tracked = {i["order_id"] for i in intents if i.get("order_id")}
            settlement_window_start = record.get(
                "close_timestamp", record["entry_cancel_at"] + (900 - CANCEL_AFTER)
            ) - SETTLEMENT_WINDOW
            def complete(item, order):
                item["entry_closed"] = True
                try:
                    released = reconcile_reservation(item, order)
                    if released:
                        write_log("ENTRY_UNUSED_ALLOWANCE_RELEASED" if released > 0 else "ENTRY_FEE_RESERVATION_INCREASED", ticker, price=item.get("price", ""),
                            details=json.dumps({"order_id": item["order_id"], "released_dollars": str(max(Decimal("0"), released)),
                                "additional_reserved_dollars": str(max(Decimal("0"), -released)),
                                "retained_dollars": item["reserved_dollars"],
                                "filled_quantity": item["confirmed_entry_filled_quantity"]}))
                except (ValueError, ArithmeticError) as error:
                    # A terminal status alone cannot prove how much filled.
                    write_log("ENTRY_ALLOWANCE_RECONCILIATION_PENDING", ticker, details=repr(error))
                for key in ("orders", "dual_limit_orders"):
                    record[key] = [oid for oid in record.get(key, []) if oid != item["order_id"]]
                for historical in record.get("historical_strike_orders", []):
                    if historical.get("order_id") == item["order_id"]:
                        historical["entry_closed"] = True
                save_state(state)

            for item in intents:
                if not item.get("order_id") or item.get("reservation_reconciled"):
                    continue
                if item.get("entry_closed") and now_timestamp >= close:
                    continue
                try:
                    order = terminal_ioc_receipt(item) if item.get("resting_entry") is False else None
                    # A partial fill still needs the fee totals before any
                    # allowance is released. Keep its full reservation if the
                    # read model has not caught up with the placement receipt.
                    partial = order and 0 < Decimal(order["fill_count_fp"]) < Decimal(item["quantity"])
                    if order is None or partial:
                        try:
                            remote = client.order(item["order_id"], ticker)
                            if (partial and remote.get("status") in {"executed", "canceled", "expired"}
                                    and Decimal(str(remote.get("fill_count_fp", remote.get("fill_count", "NaN"))))
                                    != Decimal(order["fill_count_fp"])):
                                raise ValueError("Order lookup disagrees with terminal IOC receipt")
                            if order is None or remote.get("status") in {"executed", "canceled", "expired"}:
                                order = remote
                        except KalshiAPIError as error:
                            if order is None or error.status_code != 404:
                                raise
                    if order.get("status") in {"executed", "canceled", "expired"}:
                        complete(item, order)
                except Exception as error:
                    write_log("ENTRY_STATUS_RETRY", ticker, details=repr(error))

            pending = [i for i in intents if not i.get("entry_closed")]
            ids = {i["order_id"] for i in pending if i.get("order_id") and (
                i.get("entry_execution_version") != ENTRY_EXECUTION_VERSION
                or (FINAL_TWO_MINUTES_ONLY and i.get("kind") != SETTLEMENT_KIND)
                or not tracked_entry_price_allowed(i)
                or blocked_buy_window(i.get("price", "0"),
                    record.get("close_timestamp", record["entry_cancel_at"] + (900 - CANCEL_AFTER)),
                    max(now_timestamp, time.time()))
                or now_timestamp >= i.get("cancel_at", record["entry_cancel_at"])
                or (i.get("kind") == SETTLEMENT_KIND and now_timestamp < settlement_window_start)
                or record.get("entry_budget_legacy")
                or (record.get("settlement_switch") and i.get("kind") != SETTLEMENT_KIND))}
            # A resting buy cannot remain eligible after BTC leaves the
            # qualifying side. This includes the 96c settlement limit in the
            # directional policy; cancel on an unavailable quote as well.
            scalps = [i for i in pending if i.get("resting_entry") and
                      (DIRECTIONAL_ENTRY_POLICY or i.get("kind") != SETTLEMENT_KIND)]
            if scalps:
                try:
                    live = strike_side(client.market(ticker), client.btc_reference_price(),
                                       MIN_STRIKE_DISTANCE_DOLLARS if DIRECTIONAL_ENTRY_POLICY else Decimal("0"))
                except Exception:
                    live = None
                ids.update(i["order_id"] for i in scalps if i.get("order_id") and i.get("side") != live)
            if now_timestamp >= record["entry_cancel_at"] or record.get("entry_budget_legacy") or record.get("settlement_switch"):
                legacy = set(record.get("orders", [])) | set(record.get("dual_limit_orders", []))
                legacy |= {i["order_id"] for i in record.get("historical_strike_orders", [])
                           if i.get("order_id") and not i.get("entry_closed")}
                ids |= legacy - tracked  # Tracked settlement orders keep their own close deadline.
            for order_id in sorted(ids):
                if not cancel_confirmed(order_id, ticker):
                    continue
                for key in ("orders", "dual_limit_orders"):
                    record[key] = [oid for oid in record.get(key, []) if oid != order_id]
                for item in record.get("entry_intents", []) + record.get("historical_strike_orders", []):
                    if item.get("order_id") == order_id:
                        item["entry_closed"] = True
                for item in intents:
                    if item.get("order_id") == order_id:
                        try:
                            order = client.order(order_id, ticker)
                            if order.get("status") in {"executed", "canceled", "expired"}:
                                complete(item, order)
                        except Exception as error:
                            write_log("ENTRY_STATUS_RETRY", ticker, details=repr(error))
                save_state(state)
        except Exception as error:
            # Keep retrying, but let other markets and position exits progress.
            write_log("ENTRY_RECONCILE_RETRY", ticker, details=repr(error))


def place_dual_limit_buys(record, ticker, closed, now_timestamp=None, *, state, side=None):
    """Post fixed-price entries using the active window's side selection."""
    initialize_budget(record)
    now_timestamp = time.time() if now_timestamp is None else float(now_timestamp)
    cancel_at = cancellation_deadline(closed)
    if now_timestamp >= entry_deadline(closed):
        return False
    record["dual_limit_cancel_at"] = cancel_at
    record.setdefault("dual_limit_orders", [])
    changed = True
    if side is None:
        market = client.market(ticker)
        side, _, _ = selected_entry_side(record, state, ticker, market, closed, entry_side_source(closed))
    if side not in ("YES", "NO"):
        return changed
    for side in (side,):
        for price in ENTRY_EXIT_PAIRS:
            quantity = Decimal("0")
            try:
                result, quantity = funded_entry(record, state, ticker, side, price, closed, "dual", now_timestamp,
                    order_budget=BUDGET / len(ENTRY_EXIT_PAIRS))
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

def place_historical_strike_entries(record, ticker, spot, closed, now_timestamp=None, *, state, side=None):
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
        if side is None:
            market = client.market(ticker)
            side, _, _ = selected_entry_side(record, state, ticker, market, closed, entry_side_source(closed))
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

def update_prediction(record, ticker, current, elapsed, live_side="_legacy"):
    changed = False
    while len(record["predictions"]) < len(PREDICTION_SECONDS):
        index = len(record["predictions"])
        scheduled = PREDICTION_SECONDS[index]
        if elapsed < scheduled:
            break
        prediction = (record.get("signal") or {}).get("prediction") if live_side == "_legacy" else live_side
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
            record["live_quote_confidence"] = confidence
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

def settlement_position(ticker):
    rows = [row for row in client.positions(ticker) if row.get("ticker") == ticker]
    if len(rows) > 1:
        raise ValueError("Ambiguous settlement position")
    held = Decimal(str(rows[0]["position_fp"])) if rows else Decimal("0")
    if not held.is_finite():
        raise ValueError("Invalid settlement position")
    return held


def settlement_entry(record, state, ticker, closed):
    """Rest a 96c maximum buy on the live strike side; reconcile side changes."""
    now = time.time()
    if not closed.timestamp() - SETTLEMENT_WINDOW <= now < closed.timestamp():
        return
    observed = {
        "seconds_remaining": round(closed.timestamp() - now, 3),
        "required_ask": str(SETTLEMENT_PRICE), "entry_limit": str(SETTLEMENT_PRICE), "budget_dollars": str(SETTLEMENT_BUDGET),
        "market_reserved_dollars": str(sum((Decimal(i["reserved_dollars"])
            for i in record.get("entry_intents", [])), Decimal("0"))),
        "entry_budget_legacy": bool(record.get("entry_budget_legacy")),
    }
    def report(reason, **details):
        write_log("SETTLEMENT_97_CHECK", ticker,
                  details=json.dumps({**observed, "reason": reason, **details}))

    if any(i.get("kind") == SETTLEMENT_KIND and attempt_committed(i) for i in record.get("entry_intents", [])):
        report("attempt_already_recorded")
        return  # Persisted intent prevents repeats after partial fills or lost ACKs.
    market = client.market(ticker)
    spot = client.btc_reference_price()
    side = strike_side(market, spot,
                       MIN_STRIKE_DISTANCE_DOLLARS if DIRECTIONAL_ENTRY_POLICY else Decimal("0"))
    asks = {outcome: quotes(market, outcome)[0] for outcome in ("YES", "NO")}
    observed.update(yes_ask=str(asks["YES"]), no_ask=str(asks["NO"]),
                    btc_reference=str(spot), strike=str(market["floor_strike"]))
    if time.time() >= closed.timestamp():
        report("window_closed_during_quote_read")
        return
    if side is None:
        report("btc_within_25_dollars_of_strike")
        return
    locked_side = record.get("trade_side")
    if locked_side not in ("YES", "NO"):
        locked_side = (record.get("signal") or {}).get("prediction")
    if not settlement_entry_price_allowed(asks[side]):
        report("selected_side_not_at_or_above_96", side=side, selected_ask=str(asks[side]))
        return
    held = settlement_position(ticker)
    opposite = (side == "YES" and held < 0) or (side == "NO" and held > 0)
    switch = record.get("settlement_switch")
    if switch or opposite or (locked_side and side != locked_side):
        if switch and switch.get("side") != side:
            report("switch_direction_already_selected", side=switch.get("side"))
            return
        if not switch:
            # Do not liquidate just to discover that the entry allowance is
            # already exhausted. Sales never replenish this spending ledger.
            required = entry_quantity(SETTLEMENT_PRICE, SETTLEMENT_KIND) * (SETTLEMENT_PRICE + FEE_RESERVE)
            if record.get("entry_budget_legacy") or Decimal(observed["market_reserved_dollars"]) + required > MARKET_BUDGET:
                report("switch_entry_budget_unavailable")
                return
            if opposite:
                # The loss close may yield almost nothing. Check the market's
                # actual shard before selling inventory to fund a 96c entry.
                # Do not count prospective sale proceeds: the bid can move or
                # the close can partially fill before the replacement order.
                try:
                    available = Decimal(client.market_cash(ticker)["cash_dollars"])
                    if not available.is_finite() or available < 0:
                        raise ValueError("Invalid switch cash")
                except Exception as error:
                    report("switch_cash_unavailable", error_type=type(error).__name__)
                    return
                if available < required:
                    report("switch_cash_insufficient", available_dollars=str(available),
                           required_dollars=str(required))
                    return
            switch = {"side": side, "previous_side": locked_side, "phase": "requested",
                      "requested_at": time.time(), "allow_loss": True}
            record["settlement_switch"] = switch
            save_state(state)
            write_log("SETTLEMENT_SWITCH_REQUESTED", ticker, prediction=side,
                      details=json.dumps(switch))
        reconcile_entries(state)  # Confirm all prior buys are terminal first.
        if EXIT_MONITOR is not None:
            EXIT_MONITOR.wake()
        if EXIT_MONITOR is None or not EXIT_MONITOR.settlement_ready(ticker, side) or opposite:
            report("waiting_for_opposite_close_confirmation", side=side, held=str(held))
            return
        if switch.get("phase") != "ready":
            switch["phase"] = "ready"
            record["trade_side"] = side
            save_state(state)
            write_log("SETTLEMENT_SWITCH_READY", ticker, prediction=side)
    elif locked_side is None:
        record["trade_side"] = side
        save_state(state)
    # Reconciliation must cancel earlier entry orders before switching sides.
    pending = [i for i in record.get("entry_intents", []) if not i.get("entry_closed")]
    if any(i.get("side") != side for i in pending):
        report("unresolved_opposite_entry", side=side)
        return
    report("eligible_for_funding_check", side=side)
    result, quantity = funded_entry(record, state, ticker, side, SETTLEMENT_PRICE, closed,
        SETTLEMENT_KIND, submit_before=closed.timestamp(), cancel_at=closed.timestamp())
    if result.get("order_id"):
        write_log("SETTLEMENT_97_ENTRY", ticker, prediction=side, price=str(SETTLEMENT_PRICE), quantity=str(quantity),
                  details=json.dumps({"budget": str(SETTLEMENT_BUDGET), "hold_to_settlement": True,
                                      "order": result}))
    else:
        report("not_submitted_or_unacknowledged", side=side,
               cash_retry_at=record.get("cash_retry_at"), quantity=str(quantity))


def reconcile_sale_allowance(state, ticker, record):
    """Only independently confirmed, fill-backed sells can restore allowance."""
    path = getattr(EXIT_MONITOR, "path", None)
    if path is None or not path.exists():
        return
    try:
        ledger = json.loads(path.read_text()).get("markets", {}).get(ticker, {})
        exits = ledger.get("exit_orders", {})
        snapshot = {oid: str(item.get("filled")) for oid, item in exits.items()
                    if item.get("purpose") == "take_profit" and item.get("allocations")}
        if snapshot == record.get("recycling_exit_snapshot", {}) and record.get("recycling_checked"):
            return
        credits = confirmed_credits(record, exits, client.all_fills(ticker), ticker)
        previous = record.get("recycled_exit_orders", {})
        if any(credits.get(oid) != amount for oid, amount in previous.items()):
            raise ValueError("Previously credited sale is absent or changed")
        spent = sum((Decimal(item["reserved_dollars"]) for item in record.get("entry_intents", [])), Decimal(0))
        if sum((Decimal(amount) for amount in credits.values()), Decimal(0)) > spent:
            raise ValueError("Confirmed sale credit exceeds entry reservations")
        record["recycled_exit_orders"] = credits
        record["recycling_exit_snapshot"] = snapshot
        record["recycling_checked"] = True
        save_state(state)
        additional = sum((Decimal(credits[oid]) for oid in credits.keys() - previous.keys()), Decimal(0))
        if additional:
            write_log("ENTRY_SALE_ALLOWANCE_RESTORED", ticker, details=json.dumps({
                "new_credit_dollars": str(additional),
                "available_earlier_dollars": str(remaining_allowance(record, MARKET_BUDGET, "regular")),
                "confirmed_exit_orders": len(credits)}))
    except Exception as error:
        # Existing reservations stay charged. A lagging or inconsistent exchange
        # read never frees budget and does not stop the independent exit worker.
        if record.get("recycling_error") != repr(error):
            record["recycling_error"] = repr(error)
            save_state(state)
            write_log("ENTRY_SALE_ALLOWANCE_PENDING", ticker, details=repr(error))


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
    record = state["markets"].setdefault(ticker, {"buys": 0, "last_buy": 0, "signal": None, "predictions": [], "orders": [], "spot_entry_attempted": False, "final_entry_attempted": False, "dual_limit_attempted": False, "dual_limit_orders": [], "historical_strike_orders": [], "historical_triggered_strikes": [], "historical_take_profit_orders": [], "late_entry_attempted": False})
    if "predictions" not in record: record["predictions"] = []
    if "spot_entry_attempted" not in record: record["spot_entry_attempted"] = False
    if "opening_bias_attempted" not in record: record["opening_bias_attempted"] = False
    if "final_entry_attempted" not in record: record["final_entry_attempted"] = False
    if "dual_limit_attempted" not in record: record["dual_limit_attempted"] = False
    if "late_entry_attempted" not in record: record["late_entry_attempted"] = False
    if "dual_limit_orders" not in record: record["dual_limit_orders"] = []
    if "historical_strike_orders" not in record: record["historical_strike_orders"] = []
    if "historical_triggered_strikes" not in record: record["historical_triggered_strikes"] = []
    if "historical_take_profit_orders" not in record: record["historical_take_profit_orders"] = []
    initialize_budget(record)
    record["entry_cancel_at"] = cancellation_deadline(closed)
    record["close_timestamp"] = closed.timestamp()
    save_state(state)
    reconcile_entries(state)
    reconcile_sale_allowance(state, ticker, record)
    if time.time() < started.timestamp() + ENTRY_START_DELAY:
        write_log("ENTRY_START_WAIT", ticker, details="Buys begin one minute after market open")
        return  # Preserve all entry opportunities while reconciliation continues.
    if closed.timestamp() - SETTLEMENT_WINDOW <= time.time() < closed.timestamp():
        settlement_entry(record, state, ticker, closed)
        return
    if FINAL_TWO_MINUTES_ONLY:
        write_log("ENTRY_FINAL_WINDOW_ONLY_WAIT", ticker, details=json.dumps({
            "seconds_remaining": max(0, round(closed.timestamp() - time.time(), 3)),
            "allowed_window_seconds": SETTLEMENT_WINDOW,
            "only_allowed_entry_kind": SETTLEMENT_KIND,
        }))
        return
    if EXIT_MONITOR is not None and not EXIT_MONITOR.healthy:
        write_log("ENTRY_WAIT_TAKE_PROFIT", ticker, details="Exit monitor warming up or recovering")
        return
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
    current = client.market(ticker)
    side_source = entry_side_source(closed)
    elapsed = time.time() - started.timestamp()
    try:
        selected_side, live_side, _ = selected_entry_side(record, state, ticker, current, closed, side_source)
    except Exception as error:
        write_log("ENTRY_SIDE_UNAVAILABLE", ticker, details=repr(error))
        if ENTRY_START_DELAY <= elapsed < OPENING_55_WINDOW:
            selected_side, live_side = None, None
        else:
            return
    signal = (record["entry_bias"] if side_source == "boruto" else
              {"prediction": selected_side, "base_confidence": side_source.upper()})
    write_log("ENTRY_SIDE", ticker, prediction=selected_side or "AT_STRIKE",
              details=json.dumps({"side_source": side_source, "live_strike_side": live_side,
                                  "strike": str(current["floor_strike"])}))
    opening_55_submitted = False
    if ENTRY_START_DELAY <= elapsed < OPENING_55_WINDOW:
        candidates = []
        for side in ("YES", "NO"):
            ask, _ = quotes(current, side)
            if ask.is_finite() and MIN_ENTRY_PRICE <= ask <= Decimal("0.55"):
                candidates.append((ask, side))
        if candidates:
            # Price alone selects the side: prefer the qualifying ask closest
            # to 55c. If both books are identical, wait instead of inventing
            # a directional tie-break.
            best_ask = max(ask for ask, _ in candidates)
            best = [(ask, side) for ask, side in candidates if ask == best_ask]
            if len(best) == 1:
                ask, side = best[0]
                price, target = next(iter(OPENING_55_PAIR.items()))
                result, quantity = funded_entry(
                    record, state, ticker, side, price, closed, "opening_55",
                    submit_before=started.timestamp() + OPENING_55_WINDOW,
                    cancel_at=started.timestamp() + OPENING_55_WINDOW,
                )
                if result.get("order_id"):
                    opening_55_submitted = True
                    record["orders"].append(result["order_id"])
                write_log("OPENING_55_LIMIT", ticker, prediction=side, price=str(price),
                          quantity=str(quantity), details=json.dumps({
                              "observed_ask": str(ask), "exit_target": str(target),
                              "entry_cutoff": started.timestamp() + OPENING_55_WINDOW,
                              "order": result,
                          }))
                save_state(state)
            else:
                write_log("OPENING_55_AMBIGUOUS", ticker, details=json.dumps({
                    "qualifying_asks": {side: str(ask) for ask, side in candidates},
                    "action": "skip_tie",
                }))
    # Every opening route follows the opposite live strike side until 2:00.
    if (not opening_55_submitted and OPENING_BIAS_ENABLED
            and ENTRY_START_DELAY <= elapsed < OPENING_WINDOW and signal["prediction"] in ("YES", "NO")):
        price, target = next(iter(OPENING_BIAS_PAIR.items()))
        result, quantity = funded_entry(record, state, ticker, selected_side, price, closed, "opening_bias",
            submit_before=started.timestamp() + OPENING_WINDOW, cancel_at=started.timestamp() + OPENING_WINDOW)
        if result.get("order_id"):
            record["orders"].append(result["order_id"])
            record["opening_bias_attempted"] = True
        write_log("OPENING_BIAS_LIMIT", ticker, prediction=signal["prediction"], price=str(price), quantity=str(quantity),
                  details=json.dumps({"exit_target": str(target), "entry_cutoff": started.timestamp() + OPENING_WINDOW, "order": result}))
        save_state(state)
    # An independent opening attempt is required: the legacy 52c attempt flag
    # must not consume the 57c opportunity. A quote/cash wait creates no intent,
    # so this route can try again while its two-minute window remains open.
    if (not opening_55_submitted
            and ENTRY_START_DELAY <= time.time() - started.timestamp() < OPENING_EXTRA_WINDOW
            and selected_side in ("YES", "NO")):
        for price, target in OPENING_EXTRA_PAIR.items():
            result, quantity = funded_entry(record, state, ticker, selected_side, price, closed, "opening_57",
                submit_before=started.timestamp() + OPENING_EXTRA_WINDOW,
                cancel_at=started.timestamp() + OPENING_EXTRA_WINDOW)
            if result.get("order_id"):
                record["orders"].append(result["order_id"])
                write_log("OPENING_57_LIMIT", ticker, prediction=selected_side, price=str(price), quantity=str(quantity),
                          details=json.dumps({"exit_target": str(target), "entry_cutoff": started.timestamp() + OPENING_EXTRA_WINDOW, "order": result}))
                save_state(state)
    if selected_side in ("YES", "NO") and update_prediction(record, ticker, current, elapsed, selected_side):
        save_state(state)
    reconcile_entries(state)
    # Only the independent paired monitor owns exits. Never fall back to a
    # single-price exit path when both entry tiers can hold inventory.
    can_buy = (not opening_55_submitted and START <= elapsed < END
               and selected_side in ("YES", "NO")
               and time.time() - record["last_buy"] >= INTERVAL)
    if can_buy:
        ask, _ = quotes(current, selected_side)
        counted = False
        for price, result, quantity in paired_entries(record, state, ticker, selected_side, closed, "regular"):
            if result.get("order_id"):
                if not counted:
                    record["buys"] += 1; record["last_buy"] = time.time()
                    counted = True
                record["orders"].append(result["order_id"])
                write_log("BUY_LIMIT", ticker, prediction=signal["prediction"], confidence=signal.get("live_confidence", ""), price=str(price), quantity=str(quantity), details=f"batch {record['buys']}; committed regular orders {committed_regular_orders(record)}; tier {price}; order {result['order_id']}")
                save_state(state)
    late_start = started.timestamp() + LATE_ENTRY_START
    late_end = started.timestamp() + LATE_ENTRY_END
    if late_start <= time.time() < late_end and selected_side in ("YES", "NO"):
        for price, result, quantity in paired_late_entries(
            record, state, ticker, selected_side, closed, time.time(), late_end, late_end
        ):
            if result.get("order_id"):
                record["orders"].append(result["order_id"])
                record["late_entry_attempted"] = True
            write_log(
                "LATE_BIAS_LIMIT", ticker, prediction=signal["prediction"], price=str(price),
                quantity=str(quantity), details=json.dumps({
                    "exit_target": str(LATE_ENTRY_PAIRS[price]), "entry_start": late_start,
                    "entry_cutoff": late_end, "cancel_at": late_end, "order": result,
                }),
            )
            save_state(state)
    if DUAL_LIMIT_BUYS_ENABLED and START <= elapsed < END and selected_side in ("YES", "NO") and not record["dual_limit_attempted"]:
        record["dual_limit_attempted"] = True
        save_state(state)
        if place_dual_limit_buys(record, ticker, closed, state=state, side=selected_side): save_state(state)
    if HISTORICAL_STRIKE_ENABLED and START <= elapsed < END and record.get("historical_strikes"):
        try:
            reference_spot = client.btc_reference_price()
            if place_historical_strike_entries(record, ticker, reference_spot, closed, state=state, side=selected_side): save_state(state)
        except Exception as error:
            if not record.get("historical_spot_error_logged"):
                record["historical_spot_error_logged"] = True
                write_log("HISTORICAL_SPOT_UNAVAILABLE", ticker, details=repr(error))
                save_state(state)
    if (ENTRY_START_DELAY <= elapsed < SPOT_ENTRY_WINDOW and selected_side in ("YES", "NO")
            and (selected_side == "YES" or side_source == "opposite_strike") and not record["spot_entry_attempted"]):
        spot = client.btc_reference_price(); strike = Decimal(str(current["floor_strike"]))
        if spot_is_above_strike(spot, strike, SPOT_ENTRY_THRESHOLD):
            ask, _ = quotes(current, selected_side)
            if Decimal("0") < ask <= Decimal("1"):
                record["spot_entry_attempted"] = True; save_state(state)
                for price, result, quantity in paired_entries(record, state, ticker, selected_side, closed, "spot", submit_before=started.timestamp() + SPOT_ENTRY_WINDOW):
                    if result.get("order_id"): record["orders"].append(result["order_id"])
                    save_state(state)
                    details = {"spot": str(spot), "strike": str(strike), "distance_above_strike": str(spot - strike), "order": result}
                    write_log("SPOT_TRIGGER_BUY", ticker, prediction=selected_side, confidence=live_confidence(ask), price=str(price), quantity=str(quantity), details=json.dumps(details))

def check():
    balance = client.balance(); markets = client.markets(series_ticker="KXBTC15M", status="open", limit=1)
    print(json.dumps({"authenticated": True, "balance_dollars": balance.get("balance_dollars"), "KXBTC15M_visible": bool(markets)}, indent=2))

def main():
    global EXIT_MONITOR
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    version = Path(__file__).with_name("VERSION").read_text().strip()
    print(f"Strike Ruler bot v{version}; execution={EXECUTION_STRATEGY}; entries use live BTC at least ${MIN_STRIKE_DISTANCE_DOLLARS} beyond strike: above=YES, below=NO", flush=True)
    print(f"BUY_POLICY: only settlement entries are enabled in the final {SETTLEMENT_WINDOW}s; all opening, regular, historical, spot, dual, and late entry routes are disabled; exits continue", flush=True)
    print(f"SETTLEMENT_ENTRY window={900 - SETTLEMENT_WINDOW}s..900s; live strike side; trigger_ask>=96c and <100c; limit=96c GTC until close; budget=$6 reserved; quantity=6; confirm opposite close even at loss before buying; hold to settlement", flush=True)
    print("BUY_ORDER_CLEANUP: pending bot entry orders from disabled routes are canceled", flush=True)
    print("ENTRY_FUNDING market exchange_index cash required; insufficient funds retry after 30s; no automatic transfers", flush=True)
    ignored = ("ENTRY_BUDGET_DOLLARS", "TAKE_PROFIT_CENTS", "TAKE_PROFIT_PERCENT", "STOP_EXIT_CENTS",
               "ENTRY_MIN_CENTS", "ENTRY_MAX_CENTS", "ENTRY_PRICE_CENTS", "EXIT_PRICE_CENTS",
               "FINAL_ENTRY_START_MINUTE", "FINAL_ENTRY_END_MINUTE", "FINAL_CONFIDENCE_MIN_PERCENT")
    for name in ignored:
        if name in os.environ:
            print(f"CONFIG_IGNORED: {name}; fixed entry sizing and paired prices apply; no stop-loss is active", flush=True)
    if "ENTRY_START_MINUTE" in os.environ or "ENTRY_END_MINUTE" in os.environ:
        print(f"CONFIG_IGNORED: fixed entry policy: only settlement buys from{(900 - SETTLEMENT_WINDOW) // 60}m through market close", flush=True)
    if os.getenv("PREDICTION_UPDATE_MINUTES", "2,4,6") != "2,4,6":
        print("CONFIG_IGNORED: prediction schedule is fixed at 2,4,6 minutes", flush=True)
    if args.check:
        log_api_cash(client)
        check()
        return
    if not ENABLED:
        log_api_cash(client)
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
            os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""), timeout=5,
            coordinator=REQUEST_COORDINATOR, role="exit")
        EXIT_MONITOR = TakeProfitMonitor(exit_client, load_state,
            STATE.with_name(STATE.stem + "_take_profit.json"), pairs=ALL_ENTRY_EXIT_PAIRS,
            poll=float(os.getenv("EXIT_POLL_SECONDS", "1")), fill_cost_targets=True,
            per_order_profit=PER_ORDER_PROFIT_DOLLARS, force_exit_price=Decimal("0.98"),
            no_fill_pause=3.0, quote_gate=True)
        EXIT_MONITOR.start()
        print(f"TP_MONITOR_STARTED gross_profit_goal=${PER_ORDER_PROFIT_DOLLARS:.2f} per buy order total, target price=actual average fill cost + goal/remaining contracts rounded up; before fees; saved pair fallback if above 99c; 98c sell override; quote-gated reduce-only IOC exits; 3s pause after zero fill", flush=True)
        diagnostics_client = KalshiClient(os.getenv("KALSHI_API_KEY_ID", ""),
            os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""), timeout=5,
            coordinator=REQUEST_COORDINATOR, role="diagnostics")
        balance_monitor = BalanceMonitor(diagnostics_client)
        balance_monitor.start()
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
            balance_monitor.stop()
            if EXIT_MONITOR is not None:
                EXIT_MONITOR.stop()

if __name__ == "__main__": main()
