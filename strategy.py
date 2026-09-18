from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

@dataclass(frozen=True)
class Signal:
    prediction: str
    confidence: str
    moves: tuple
    flipped: bool

def strike_ruler(points, absolute_gap_average):
    p = tuple(Decimal(str(x)) for x in points)
    if len(p) != 4:
        raise ValueError("strike_ruler requires three completed prices and the current strike")

    prior_prices = p[:3]
    strike = p[3]
    gaps = tuple(price - strike for price in prior_prices)
    below = sum(price < strike for price in prior_prices)
    above = sum(price > strike for price in prior_prices)

    if below >= 2:
        prediction = "YES"
    elif above >= 2:
        prediction = "NO"
    else:
        return Signal("SKIP", "NONE", gaps, False)

    confidence = "HIGH" if below == 3 or above == 3 else "MODERATE"
    return Signal(prediction, confidence, gaps, False)

def live_confidence(predicted_side_ask):
    price = Decimal(str(predicted_side_ask))
    if price < 0 or price > 1:
        raise ValueError("Kalshi contract price must be between 0 and 1")
    return f"{(price * 100).quantize(Decimal('0.1'))}%"

def seconds_from_minutes(value):
    """Convert whole or decimal minutes to an exact whole-second offset."""
    seconds = Decimal(str(value)) * Decimal("60")
    if seconds < 0 or seconds != seconds.to_integral_value():
        raise ValueError("minute settings must resolve to a non-negative whole number of seconds")
    return int(seconds)

def fixed_take_profit_target(average_entry, take_profit_cents):
    """Return a fixed-cent contract target, capped at the $1 settlement value."""
    entry = Decimal(str(average_entry))
    cents = Decimal(str(take_profit_cents))
    if entry < 0 or entry > 1 or cents < 0:
        raise ValueError("entry must be between 0 and 1 and take-profit cents cannot be negative")
    return min(Decimal("1"), entry + cents / Decimal("100"))

def average_prediction_confidence(predictions):
    """Average the predicted-side ask prices captured in prediction snapshots."""
    if not predictions:
        return None
    prices = [Decimal(str(snapshot["ask"])) for snapshot in predictions]
    if any(price < 0 or price > 1 for price in prices):
        raise ValueError("Kalshi contract price must be between 0 and 1")
    return sum(prices, Decimal("0")) / Decimal(len(prices))

def spot_is_above_strike(spot_price, strike_price, threshold=Decimal("80")):
    return Decimal(str(spot_price)) >= Decimal(str(strike_price)) + Decimal(str(threshold))

def quantity_for_budget(price, budget=Decimal("0.77")):
    price = Decimal(str(price))
    if price <= 0:
        raise ValueError("price must be positive")
    return (Decimal(str(budget))/price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def average_open_price(fills, outcome_side):
    """Return the average-cost basis of the currently open YES or NO inventory."""
    outcome_side = outcome_side.upper()
    if outcome_side not in ("YES", "NO"):
        raise ValueError("outcome_side must be YES or NO")

    quantity = Decimal("0")
    cost = Decimal("0")
    for fill in sorted(fills, key=lambda item: item.get("created_time", "")):
        fill_side = str(fill.get("outcome_side") or "").upper()
        if not fill_side:
            legacy_side = str(fill.get("side") or "").upper()
            action = str(fill.get("action") or "").lower()
            if legacy_side in ("YES", "NO") and action == "buy":
                fill_side = legacy_side
            elif legacy_side in ("YES", "NO") and action == "sell":
                fill_side = "NO" if legacy_side == "YES" else "YES"
        count = Decimal(str(fill.get("count_fp") or fill.get("count") or "0"))
        if count <= 0 or fill_side not in ("YES", "NO"):
            continue
        if fill_side == outcome_side:
            price_key = "yes_price_dollars" if outcome_side == "YES" else "no_price_dollars"
            price = Decimal(str(fill.get(price_key) or "0"))
            if price <= 0:
                continue
            quantity += count
            cost += count * price
        elif quantity > 0:
            removed = min(count, quantity)
            cost -= (cost / quantity) * removed
            quantity -= removed

    return cost / quantity if quantity > 0 else None
