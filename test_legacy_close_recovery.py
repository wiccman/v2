"""Offline startup migration tests. All sockets are blocked; no exchange writes."""
import copy
import hashlib
import json
import socket
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import legacy_close_recovery as recovery

NOW = '2026-10-03T00:45:00Z'
CLOSE = datetime.fromisoformat(NOW.replace('Z', '+00:00')).timestamp()


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Network forbidden in startup tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)


class APIError(Exception):
    def __init__(self, status, retry_after=None):
        self.status_code = status
        self.retry_after = retry_after


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
    def request(self, method, path):
        assert method == 'GET'
        assert path.startswith(('/markets/', '/historical/markets/'))
        self.calls.append((method, path))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def market(ticker='T', close=NOW):
    return {'market': {'ticker': ticker, 'close_time': close}}


def state():
    return {'markets': {'T': {'buys': 1, 'entry_intents': [
        {'reserved_dollars': '10.89', 'order_id': 'owned', 'client_id': 'same',
         'quantity': '11', 'entry_closed': False}], 'orders': ['owned'],
        'manual_fill_baseline': ['manual'], 'recycled_exit_orders': {}}},
        'other_version_state': {'untouched': True}}


def plan(snapshot, client, **kwargs):
    return recovery.plan_recovery(snapshot, client, log=lambda *a, **k: None,
                                  sleep=lambda seconds: None, **kwargs)


@pytest.mark.parametrize('value', [None, '', 'NaN', 'Infinity', float('inf'),
    float('nan'), -1, 0, True, False, [], {}, 10**20, CLOSE * 1000])
def test_invalid_numeric_close(value):
    assert recovery.numeric_close(value) is None


@pytest.mark.parametrize('value', [CLOSE, int(CLOSE), str(CLOSE)])
def test_numeric_close(value):
    assert recovery.numeric_close(value) == CLOSE


def test_missing_close_resolves_without_mutating_input():
    snapshot = state()
    original = copy.deepcopy(snapshot)
    client = FakeClient([market()])
    prepared, changes = plan(snapshot, client)
    assert snapshot == original
    assert changes == {'T': CLOSE}
    original['markets']['T']['close_timestamp'] = CLOSE
    assert prepared == original
    assert client.calls == [('GET', '/markets/T')]


def test_valid_saved_close_requires_no_network():
    snapshot = state()
    snapshot['markets']['T']['close_timestamp'] = CLOSE
    prepared, changes = plan(snapshot, FakeClient([]))
    assert prepared == snapshot and not changes


def test_normalizes_numeric_string_only():
    snapshot = state()
    snapshot['markets']['T']['close_timestamp'] = str(CLOSE)
    prepared, changes = plan(snapshot, FakeClient([]))
    assert changes == {'T': CLOSE}
    assert isinstance(prepared['markets']['T']['close_timestamp'], float)


def test_empty_unowned_records_left_alone():
    snapshot = {'markets': {'empty': {'buys': 0, 'orders': [], 'entry_intents': []}}}
    assert plan(snapshot, FakeClient([])) == (snapshot, {})


def test_historical_fallback_only_on_404():
    client = FakeClient([APIError(404), market()])
    assert recovery.fetch_close(client, 'T') == CLOSE
    assert client.calls == [('GET', '/markets/T'), ('GET', '/historical/markets/T')]


@pytest.mark.parametrize('status', [401, 403, 429, 500, 503])
def test_other_status_not_treated_as_closed(status):
    client = FakeClient([APIError(status)])
    with pytest.raises(recovery.RecoveryPending):
        plan(state(), client)
    assert len(client.calls) == 1


def test_historical_404_remains_blocked():
    client = FakeClient([APIError(404), APIError(404)])
    snapshot = state()
    with pytest.raises(recovery.RecoveryPending):
        plan(snapshot, client)
    assert 'close_timestamp' not in snapshot['markets']['T']


@pytest.mark.parametrize('response', [market('WRONG'), {'market': {}}, {},
    market(close=None), market(close='2026-10-03T00:45:00'), market(close='garbage'),
    market(close='1969-01-01T00:00:00Z')])
def test_invalid_metadata_blocks(response):
    with pytest.raises(recovery.RecoveryPending):
        plan(state(), FakeClient([response]))


def test_timezone_offset_is_resolved_not_assumed():
    assert recovery.metadata_close(market(close='2026-10-02T19:45:00-05:00')['market'], 'T') == CLOSE


def test_retry_after_honored():
    with pytest.raises(recovery.RecoveryPending) as caught:
        plan(state(), FakeClient([APIError(429, 75)]))
    assert caught.value.retry_after == 75


def test_partial_plan_not_committed_and_cache_avoids_repeat_get():
    snapshot = state()
    snapshot['markets']['U'] = {'buys': 1}
    cache = {}
    with pytest.raises(recovery.RecoveryPending):
        plan(snapshot, FakeClient([market(), TimeoutError()]), cache=cache)
    assert 'close_timestamp' not in snapshot['markets']['T']
    client = FakeClient([market('U')])
    prepared, changes = plan(snapshot, client, cache=cache)
    assert changes == {'T': CLOSE, 'U': CLOSE}
    assert client.calls == [('GET', '/markets/U')]


def test_exact_byte_backup_and_metadata_only_update(tmp_path):
    path = tmp_path / 'state.json'
    original = json.dumps(state(), indent=1).encode()
    path.write_bytes(original)
    prepared, changes = plan(state(), FakeClient([market()]))
    backup = recovery.commit_recovery(path, original, prepared, changes)
    assert backup.read_bytes() == original
    assert json.loads(path.read_bytes()) == prepared
    assert hashlib.sha256(original).hexdigest() in backup.name
    after = path.read_bytes()
    assert recovery.commit_recovery(path, after, prepared, {}) is None
    assert path.read_bytes() == after


def test_budget_or_order_changes_rejected(tmp_path):
    path = tmp_path / 'state.json'
    original = json.dumps(state()).encode()
    path.write_bytes(original)
    prepared, changes = plan(state(), FakeClient([market()]))
    prepared['markets']['T']['entry_intents'][0]['reserved_dollars'] = '0'
    with pytest.raises(recovery.RecoveryPending):
        recovery.commit_recovery(path, original, prepared, changes)
    assert path.read_bytes() == original


def test_concurrent_ledger_change_never_overwritten(tmp_path):
    path = tmp_path / 'state.json'
    original = json.dumps(state()).encode()
    path.write_bytes(original + b'\n')
    prepared, changes = plan(state(), FakeClient([market()]))
    with pytest.raises(recovery.RecoveryPending):
        recovery.commit_recovery(path, original, prepared, changes)
    assert path.read_bytes() == original + b'\n'


def test_backup_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    original = json.dumps(state()).encode()
    path.write_bytes(original)
    prepared, changes = plan(state(), FakeClient([market()]))
    def fail(fd):
        raise OSError('disk failure')
    monkeypatch.setattr(recovery.os, 'fsync', fail)
    with pytest.raises(OSError):
        recovery.commit_recovery(path, original, prepared, changes)
    assert path.read_bytes() == original


def fake_legacy(path):
    return SimpleNamespace(STATE=path,
        validate_storage=lambda: json.loads(path.read_bytes()),
        load_state=lambda: json.loads(path.read_bytes()))


def test_real_file_recovery_and_readback(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state()))
    result = recovery.recover_once(fake_legacy(path), FakeClient([market()]), cache={},
        log=lambda *a, **k: None, sleep=lambda seconds: None)
    assert result['repaired_records'] == 1
    assert result['order_requests_sent'] == 0
    assert result['spending_ledger_reset'] is False
    assert json.loads(path.read_text())['markets']['T']['close_timestamp'] == CLOSE


def test_missing_state_is_not_recreated(tmp_path):
    path = tmp_path / 'state.json'
    with pytest.raises(FileNotFoundError):
        recovery.recover_once(fake_legacy(path), FakeClient([]), cache={})
    assert not path.exists()


def test_entrypoint_handoff_preserves_disabled_switch(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state()))
    client = FakeClient([market()])
    monkeypatch.setitem(sys.modules, 'bot', fake_legacy(path))
    monkeypatch.setitem(sys.modules, 'kalshi', SimpleNamespace(KalshiClient=lambda **k: client))
    monkeypatch.setattr(sys, 'argv', ['legacy_close_recovery.py', '--start-bot'])
    monkeypatch.setenv('TRADING_ENABLED', 'false')
    monkeypatch.setattr(recovery.time, 'sleep', lambda s: None)
    monkeypatch.setattr(recovery.signal, 'signal', lambda *a: None)
    executions = []
    monkeypatch.setattr(recovery.os, 'execv', lambda executable, args: executions.append(args))
    recovery.main()
    assert len(executions) == 1
    assert executions[0][-2].endswith('two_rule_bot.py')
    assert executions[0][-1] == '--live'
    assert recovery.os.environ['TRADING_ENABLED'] == 'false'
    assert len(client.calls) == 1


def test_unresolved_record_never_hands_off(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state()))
    client = FakeClient([APIError(404), APIError(404)])
    monkeypatch.setitem(sys.modules, 'bot', fake_legacy(path))
    monkeypatch.setitem(sys.modules, 'kalshi', SimpleNamespace(KalshiClient=lambda **k: client))
    monkeypatch.setattr(sys, 'argv', ['legacy_close_recovery.py', '--start-bot'])
    monkeypatch.setattr(recovery.signal, 'signal', lambda *a: None)
    def stop_sleep(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(recovery.time, 'sleep', stop_sleep)
    def no_exec(*a):
        raise AssertionError('Unresolved state must not start the bot')
    monkeypatch.setattr(recovery.os, 'execv', no_exec)
    recovery.main()
    assert 'close_timestamp' not in json.loads(path.read_text())['markets']['T']
