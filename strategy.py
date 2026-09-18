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

def quantity_for_budget(price, budget=Decimal("0.77")):
    price = Decimal(str(price))
    if price <= 0:
        raise ValueError("price must be positive")
    return (Decimal(str(budget))/price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
