from decimal import Decimal
import pytest
from manual_limit_exits import ManualLimitExitWorker, ManualExitBlocked


class FakeClient:
    def __init__(self):
        self.sent = []
        self.remote = []

    def _order(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        result = {"order_id": "order-1"}
        self.remote.append({"order_id": "order-1",
                            "client_order_id": kwargs["client_order_id"],
                            "status": "resting"})
        return result

    def all_orders(self, ticker):
        return self.remote


def make(tmp_path):
    client = FakeClient()
    return ManualLimitExitWorker(client, tmp_path / "manual.json"), client


def plan(worker, **kw):
    args = dict(fill_id="fill-1", ticker="T", side="yes", quantity=10,
                entry_price=".40", close_timestamp=99999,
                ownership_verified=True, available_unreserved=10)
    args.update(kw)
    return worker.plan(**args)


def test_manual_sell_is_gtc_reduce_only_and_targets_25c(tmp_path):
    worker, client = make(tmp_path)
    assert Decimal(plan(worker)["target"]) == Decimal(".43")
    worker.submit("fill-1", now=100, ownership_verified=True, available_unreserved=10)
    args, kwargs = client.sent[0]
    assert args[:3] == ("T", "ask", Decimal(10))
    assert kwargs["reduce_only"] and not kwargs["ioc"]
    assert kwargs["expiration_time"] == 99999
    with pytest.raises(ManualExitBlocked):
        worker.submit("fill-1", now=101, ownership_verified=True, available_unreserved=10)
    assert len(client.sent) == 1


def test_no_inventory_proof_means_no_order(tmp_path):
    worker, client = make(tmp_path)
    with pytest.raises(ManualExitBlocked):
        plan(worker, ownership_verified=False)
    plan(worker)
    with pytest.raises(ManualExitBlocked):
        worker.submit("fill-1", now=100, ownership_verified=False)
    assert not client.sent


def test_recovery_after_unknown_ack_never_reposts(tmp_path):
    worker, client = make(tmp_path)
    plan(worker)
    def timeout(*args, **kwargs):
        raise TimeoutError("ACK lost")
    client._order = timeout
    with pytest.raises(TimeoutError):
        worker.submit("fill-1", now=100, ownership_verified=True, available_unreserved=10)
    restarted = ManualLimitExitWorker(client, worker.path)
    with pytest.raises(ManualExitBlocked):
        restarted.submit("fill-1", now=100, ownership_verified=True, available_unreserved=10)
    assert restarted.state["orders"]["fill-1"]["status"] == "ack_pending"


def test_no_sell_after_close_or_if_inventory_reserved(tmp_path):
    worker, client = make(tmp_path)
    plan(worker)
    with pytest.raises(ManualExitBlocked):
        worker.submit("fill-1", now=99999, ownership_verified=True, available_unreserved=10)
    with pytest.raises(ManualExitBlocked):
        worker.submit("fill-1", now=100, ownership_verified=True, available_unreserved=9)
    assert not client.sent


def test_no_side_sell_uses_complementary_yes_price(tmp_path):
    worker, client = make(tmp_path)
    plan(worker, side="no", quantity=5, available_unreserved=5)
    worker.submit("fill-1", now=100, ownership_verified=True, available_unreserved=5)
    args, kwargs = client.sent[0]
    assert args[1] == "bid"
    assert kwargs["reduce_only"]
    assert kwargs["expiration_time"] == 99999
