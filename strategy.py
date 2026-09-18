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
    moves = (p[1]-p[0], p[2]-p[1], p[3]-p[2])
    newest = moves[-1]
    if newest == 0:
        return Signal("SKIP", "NONE", moves, False)
    raw = "YES" if newest > 0 else "NO"
    flipped = abs(newest) > Decimal(str(absolute_gap_average))
    prediction = ("NO" if raw == "YES" else "YES") if flipped else raw
    signs = [1 if m > 0 else -1 if m < 0 else 0 for m in moves]
    confidence = "HIGH" if len(set(signs)) == 1 and not flipped else "MODERATE"
    return Signal(prediction, confidence, moves, flipped)

def quantity_for_budget(price, budget=Decimal("0.77")):
    price = Decimal(str(price))
    if price <= 0:
        raise ValueError("price must be positive")
    return (Decimal(str(budget))/price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

