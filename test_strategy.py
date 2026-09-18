from decimal import Decimal
from strategy import strike_ruler,quantity_for_budget
def test_high(): assert strike_ruler([100,110,120,125],20).confidence=="HIGH"
def test_flip(): assert strike_ruler([100,110,120,150],20).prediction=="NO"
def test_budget(): assert quantity_for_budget(Decimal("0.47"))==Decimal("1.63")

