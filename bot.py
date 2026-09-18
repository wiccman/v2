import argparse, csv, json, os, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv
from kalshi import KalshiClient
from strategy import strike_ruler, quantity_for_budget, live_confidence, average_open_price

load_dotenv()
if os.getenv("MODE", "live").lower() != "live" or os.getenv("KALSHI_ENV", "production").lower() != "production":
    raise SystemExit("This package supports live Kalshi production only")

ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
ENTRY_MIN = Decimal(os.getenv("ENTRY_MIN_CENTS", "10")) / 100
ENTRY_MAX = Decimal(os.getenv("ENTRY_MAX_CENTS", "47")) / 100
BUDGET = Decimal(os.getenv("ENTRY_BUDGET_DOLLARS", "0.77"))
MAX_BUYS = int(os.getenv("MAX_PURCHASES_PER_MARKET", "7"))
INTERVAL = int(os.getenv("ENTRY_INTERVAL_SECONDS", "7"))
START = int(os.getenv("ENTRY_START_MINUTE", "2")) * 60
END = int(os.getenv("ENTRY_END_MINUTE", "6")) * 60
PREDICTION_MINUTES = tuple(int(x.strip()) for x in os.getenv("PREDICTION_UPDATE_MINUTES", "2,4,6").split(",") if x.strip())
PREDICTION_SECONDS = tuple(minute * 60 for minute in PREDICTION_MINUTES)
STOP = Decimal(os.getenv("STOP_EXIT_CENTS", "4")) / 100
TAKE_PROFIT_RATE = Decimal(os.getenv("TAKE_PROFIT_PERCENT", "15")) / 100
ABS_GAP_AVG = Decimal(os.getenv("ABSOLUTE_GAP_AVERAGE", "59.58"))
STATE = Path(os.getenv("STATE_PATH", "data/state.json"))
LOG = Path(os.getenv("LOG_PATH", "data/trades.csv"))
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

def quotes(market, prediction):
    if prediction == "YES": return Decimal(market["yes_ask_dollars"]), Decimal(market["yes_bid_dollars"])
    return Decimal(market["no_ask_dollars"]), Decimal(market["no_bid_dollars"])

def position(ticker):
    for item in client.positions(ticker):
        if item.get("ticker") == ticker: return Decimal(str(item.get("position_fp", "0")))
    return Decimal("0")

def manage_exit(ticker, market, signal):
    held = position(ticker)
    if held == 0: return
    side = "YES" if held > 0 else "NO"; _, bid = quotes(market, side)
    if bid <= STOP:
        result = client.close_position(ticker, held, Decimal(market["yes_bid_dollars"]), Decimal(market["yes_ask_dollars"]))
        details = {"reason": "STOP", "order": result}
        write_log("SELL_MAX", ticker, prediction=side, confidence=signal.get("live_confidence", ""), price=str(bid), quantity=str(abs(held)), details=json.dumps(details))
        return
    average_entry = average_open_price(client.fills(ticker), side)
    if average_entry is None:
        write_log("EXIT_BASIS_UNAVAILABLE", ticker, prediction=side, price=str(bid), quantity=str(abs(held)))
        return
    target = min(Decimal("1"), average_entry * (Decimal("1") + TAKE_PROFIT_RATE))
    if bid >= target:
        gross_gain = (bid / average_entry - Decimal("1")) * 100
        result = client.close_position(ticker, held, Decimal(market["yes_bid_dollars"]), Decimal(market["yes_ask_dollars"]))
        details = {"reason": "TAKE_PROFIT", "average_entry": str(average_entry), "target": str(target), "gross_gain_percent": str(gross_gain), "order": result}
        write_log("SELL_MAX", ticker, prediction=side, confidence=signal.get("live_confidence", ""), price=str(bid), quantity=str(abs(held)), details=json.dumps(details))

def cancel_entries(record, ticker):
    resting = {item.get("order_id") for item in client.orders(ticker, "resting")}
    for order_id in list(record["orders"]):
        if order_id in resting: client.cancel(order_id); write_log("CANCEL_ENTRY", ticker, details=order_id)
        record["orders"].remove(order_id)

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
    record = state["markets"].setdefault(ticker, {"buys": 0, "last_buy": 0, "signal": None, "predictions": [], "orders": []})
    if "predictions" not in record: record["predictions"] = []
    if record["signal"] is None:
        signal = strike_ruler(prior_three(started) + [Decimal(str(market["floor_strike"]))], ABS_GAP_AVG)
        record["signal"] = {"prediction": signal.prediction, "base_confidence": signal.confidence, "moves": [str(x) for x in signal.moves], "flipped": signal.flipped}
        write_log("BASE_SIGNAL", ticker, prediction=signal.prediction, confidence=signal.confidence, details=json.dumps(record["signal"])); save_state(state)
    signal = record["signal"]; current = client.market(ticker)
    if update_prediction(record, ticker, current, elapsed): save_state(state)
    manage_exit(ticker, current, signal)
    if elapsed >= END and record["orders"]: cancel_entries(record, ticker); save_state(state)
    can_buy = START <= elapsed < END and signal["prediction"] in ("YES", "NO") and record["predictions"] and record["buys"] < MAX_BUYS and time.time() - record["last_buy"] >= INTERVAL
    if can_buy:
        ask, _ = quotes(current, signal["prediction"])
        if ENTRY_MIN <= ask <= ENTRY_MAX:
            quantity = quantity_for_budget(ask, BUDGET)
            result = client.place_entry(ticker, signal["prediction"], quantity, ask, started.timestamp() + END)
            if result.get("order_id"): record["orders"].append(result["order_id"])
            record["buys"] += 1; record["last_buy"] = time.time()
            write_log("BUY_LIMIT", ticker, prediction=signal["prediction"], confidence=signal.get("live_confidence", ""), price=str(ask), quantity=str(quantity), details=f"purchase {record['buys']} of {MAX_BUYS}"); save_state(state)

def check():
    balance = client.balance(); markets = client.markets(series_ticker="KXBTC15M", status="open", limit=1)
    print(json.dumps({"authenticated": True, "balance_dollars": balance.get("balance_dollars"), "KXBTC15M_visible": bool(markets)}, indent=2))

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--check", action="store_true"); args = parser.parse_args()
    print("Strike Ruler bot v0.5.0", flush=True)
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
