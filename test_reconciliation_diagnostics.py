import single_coin_minute9 as core

def test_safe_known_reason_and_unknown_redaction():
    assert core.reconciliation_error_code(RuntimeError("Unsupported or invalid fill quantity")) == "invalid_fill_quantity"
    assert core.reconciliation_error_code(RuntimeError("secret payload")) == "unclassified"
    assert core.reconciliation_error_code(ValueError("Order ownership mismatch")) == "unclassified"
