from decimal import Decimal
from strategy import strike_ruler, quantity_for_budget, live_confidence

def test_three_below_predicts_yes_high():
    signal = strike_ruler([100, 110, 120, 130], 1)
    assert signal.prediction == "YES"
    assert signal.confidence == "HIGH"
    assert signal.flipped is False

def test_two_below_predicts_yes_moderate():
    signal = strike_ruler([100, 140, 120, 130], 1)
    assert signal.prediction == "YES"
    assert signal.confidence == "MODERATE"

def test_three_above_predicts_no_high():
    signal = strike_ruler([150, 140, 135, 130], 1)
    assert signal.prediction == "NO"
    assert signal.confidence == "HIGH"

def test_two_above_predicts_no_moderate():
    signal = strike_ruler([150, 120, 140, 130], 1)
    assert signal.prediction == "NO"
    assert signal.confidence == "MODERATE"

def test_gap_average_never_flips_prediction():
    signal = strike_ruler([100, 110, 120, 10000], 1)
    assert signal.prediction == "YES"
    assert signal.flipped is False

def test_no_majority_skips():
    signal = strike_ruler([100, 130, 120, 120], 1)
    assert signal.prediction == "SKIP"
    assert signal.confidence == "NONE"

def test_live_confidence_uses_kalshi_price():
    assert live_confidence(Decimal("0.47")) == "47.0%"

def test_live_confidence_cannot_change_direction():
    signal = strike_ruler([100, 110, 120, 130], 1)
    assert signal.prediction == "YES"
    assert live_confidence(Decimal("0.12")) == "12.0%"
    assert signal.prediction == "YES"

def test_budget():
    assert quantity_for_budget(Decimal("0.47")) == Decimal("1.63")
