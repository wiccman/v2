"""Target-balance allocation regression tests; no network or credentials."""
import pytest

from kalshi import KalshiClient


def test_target_balance_allocation_posts_one_hundred_percent_to_selected_shard(monkeypatch):
    client = KalshiClient()
    calls = []

    def fake_request(method, path, params=None, body=None, auth=False):
        calls.append((method, path, params, body, auth))
        return {}

    monkeypatch.setattr(client, "request", fake_request)
    assert client.set_target_balance_allocation(2) == {}
    assert calls == [(
        "POST",
        "/portfolio/target_balance_allocation",
        None,
        {"allocations": [{"exchange_index": 2, "percent": 100}]},
        True,
    )]


@pytest.mark.parametrize("value", [-1, 101, True, 2.0, "2", None])
def test_target_balance_allocation_rejects_invalid_index_without_request(monkeypatch, value):
    client = KalshiClient()
    monkeypatch.setattr(client, "request",
                        lambda *args, **kwargs: pytest.fail("request should not be sent"))
    with pytest.raises(ValueError):
        client.set_target_balance_allocation(value)
