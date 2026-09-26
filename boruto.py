"""Opening-time 2.0 Boruto signals from finalized Kalshi market records."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

BUILD = "2.0.4 Boruto Direction Every Window"
PERIOD = timedelta(minutes=15)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone is required")
    return result


def price(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("price must be finite and positive")
    return result


def vote(points, strike):
    if len(points) != 4:
        raise ValueError("Boruto requires four completed lookbacks")
    values = [price(value) for value in points]
    strike = price(strike)
    below = sum(value < strike for value in values)
    above = sum(value > strike for value in values)
    side = "YES" if below >= 3 else "NO" if above >= 3 else None
    reason = "four_point_agreement"
    if side is None:
        # Input points are chronological. Use the newest non-equal point for
        # mixed windows; a fully flat window has a deterministic YES fallback.
        newest = next((value for value in reversed(values) if value != strike), None)
        side = "YES" if newest is None or newest < strike else "NO"
        reason = "flat_yes_fallback" if newest is None else "latest_non_equal_lookback"
    return {"bias": side, "agreement": "HIGH" if max(below, above) == 4 else
            "MODERATE" if max(below, above) == 3 else "LOW", "reason": reason,
            "below": below, "above": above, "equal": 4 - below - above}


def build_signal(target, history, now=None):
    """Validate current lookbacks; previous bias is optional context only."""
    try:
        opened = timestamp(target["open_time"])
        closed = timestamp(target["close_time"])
        now = now or datetime.now(timezone.utc)
        if closed - opened != PERIOD or not opened <= now < closed:
            raise ValueError("target must be an open 15-minute window")
        strike = price(target["floor_strike"])
        # Four current lookbacks are required; two other records are optional.
        expected = {opened - i * PERIOD for i in range(6)}
        required = {opened - i * PERIOD for i in (1, 2, 3, 4)}
        records = {}
        invalid_optional = set()
        for market in history:
            boundary = timestamp(market["close_time"])
            if boundary not in expected or market.get("status") != "finalized":
                continue
            try:
                if timestamp(market["open_time"]) != boundary - PERIOD:
                    raise ValueError("lookback is not a consecutive 15-minute window")
                item = {"ticker": market["ticker"], "boundary": boundary.isoformat(),
                        "settlement": str(price(market["expiration_value"])),
                        "strike": str(price(market["floor_strike"]))}
                if boundary in records and records[boundary] != item:
                    raise ValueError("conflicting finalized lookback records")
                records[boundary] = item
            except (KeyError, TypeError, ValueError, InvalidOperation):
                if boundary in required:
                    raise
                invalid_optional.add(boundary)
        for boundary in invalid_optional:
            records.pop(boundary, None)
        if required - records.keys():
            raise ValueError("required exact lookback periods are not finalized")
        current_points = [records[opened - i * PERIOD] for i in (4, 3, 2, 1)]
        current = vote([p["settlement"] for p in current_points], strike)
        previous_points, previous_strike, previous = [], None, None
        if opened in records and opened - 5 * PERIOD in records:
            previous_points = [records[opened - i * PERIOD] for i in (5, 4, 3, 2)]
            previous_strike = records[opened]["strike"]
            previous = vote([p["settlement"] for p in previous_points], previous_strike)
        previous_bias = previous["bias"] if previous else None
        conflict = (current["bias"] in ("YES", "NO") and previous_bias in ("YES", "NO")
                    and current["bias"] != previous_bias)
        prediction = current["bias"]
        reason = current["reason"]
        values = [price(p["settlement"]) for p in current_points] + [strike]
        return {"build": BUILD, "prediction": prediction, "base_confidence": current["agreement"],
                "current_bias": current["bias"], "previous_bias": previous_bias,
                "current": current, "previous": previous, "conflict": conflict, "reason": reason,
                "strike": str(strike), "previous_strike": previous_strike,
                "lookbacks": current_points, "previous_lookbacks": previous_points,
                "open_time": opened.isoformat(), "time_pulled_utc": now.isoformat(),
                "moves": [str(b - a) for a, b in zip(values, values[1:])], "flipped": False}
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise RuntimeError(f"DATA UNAVAILABLE: Boruto: {error}") from error
