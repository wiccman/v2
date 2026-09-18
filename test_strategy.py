from decimal import Decimal
from kalshi import _latest_index_value
from strategy import strike_ruler, quantity_for_budget, live_confidence, average_open_price, average_prediction_confidence, spot_is_above_strike

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

def test_average_prediction_confidence_uses_all_snapshots():
    predictions = [{"ask": "0.60"}, {"ask": "0.70"}, {"ask": "0.65"}]
    assert average_prediction_confidence(predictions) == Decimal("0.65")

def test_average_prediction_confidence_returns_none_without_snapshots():
    assert average_prediction_confidence([]) is None

def test_spot_trigger_at_eighty_dollars_above_strike():
    assert spot_is_above_strike("10080", "10000") is True
    assert spot_is_above_strike("10079.99", "10000") is False

def test_kalshi_cf_benchmarks_value_response():
    response = {"data": {"payload": [{"time": 1, "value": "68000.12"}, {"time": 2, "value": "68001.34"}]}}
    assert _latest_index_value(response) == Decimal("68001.34")

def test_budget():
    assert quantity_for_budget(Decimal("0.47")) == Decimal("1.63")

def test_average_open_price_uses_weighted_fills():
    fills = [
        {"created_time": "2026-01-01T00:00:00Z", "outcome_side": "yes", "action": "buy", "count_fp": "2", "yes_price_dollars": "0.40"},
        {"created_time": "2026-01-01T00:00:01Z", "outcome_side": "yes", "action": "buy", "count_fp": "1", "yes_price_dollars": "0.70"},
    ]
    assert average_open_price(fills, "YES") == Decimal("0.50")

def test_average_open_price_preserves_basis_after_partial_sale():
    fills = [
        {"created_time": "2026-01-01T00:00:00Z", "side": "no", "action": "buy", "count_fp": "4", "no_price_dollars": "0.20"},
        {"created_time": "2026-01-01T00:00:01Z", "side": "no", "action": "sell", "count_fp": "1", "no_price_dollars": "0.30"},
    ]
    assert average_open_price(fills, "NO") == Decimal("0.20")

def test_average_open_price_returns_none_without_open_inventory():
    fills = [
        {"created_time": "2026-01-01T00:00:00Z", "side": "yes", "action": "buy", "count_fp": "1", "yes_price_dollars": "0.40"},
        {"created_time": "2026-01-01T00:00:01Z", "side": "yes", "action": "sell", "count_fp": "1", "yes_price_dollars": "0.60"},
    ]
    assert average_open_price(fills, "YES") is None
