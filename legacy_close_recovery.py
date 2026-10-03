"""Repair legacy close-time metadata before launching the unchanged two-rule bot.

Only exact market-metadata GETs are permitted here. No orders, balances, fills,
state resets, inferred ticker times, or strategy changes. Original ledger bytes
are backed up before atomically updating close_timestamp fields. Unknown markets
remain blocked; HTTP errors are not evidence of settlement or cancellation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

VERSION = 'legacy-close-recovery-v1'


class RecoveryPending(RuntimeError):
    def __init__(self, ticker: str, reason: str, retry_after: float = 30):
        super().__init__(reason)
        self.ticker = ticker
        self.retry_after = retry_after


def emit(event: str, **details: Any) -> None:
    print(json.dumps({'event': event, 'time_utc': datetime.now(timezone.utc).isoformat(),
                      'recovery_version': VERSION, **details}, allow_nan=False), flush=True)


def numeric_close(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            return None
        datetime.fromtimestamp(result, timezone.utc)  # Reject milliseconds/overflow.
        return result
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def metadata_close(market: Any, ticker: str) -> float:
    if not isinstance(market, dict) or market.get('ticker') != ticker:
        raise RecoveryPending(ticker, 'Market metadata identity mismatch')
    value = market.get('close_time')
    if not isinstance(value, str):
        raise RecoveryPending(ticker, 'Market close_time unavailable')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('Timezone required')
        result = numeric_close(parsed.timestamp())
        if result is None:
            raise ValueError('Invalid close timestamp')
        return result
    except (ValueError, OverflowError, OSError) as error:
        raise RecoveryPending(ticker, 'Invalid timezone-aware market close_time') from error


def fetch_close(client: Any, ticker: str) -> float:
    """A 404 gets one documented historical-metadata fallback, never a guess."""
    escaped = quote(ticker, safe='')
    try:
        payload = client.request('GET', '/markets/' + escaped)
    except Exception as error:
        if getattr(error, 'status_code', None) != 404:
            raise
        payload = client.request('GET', '/historical/markets/' + escaped)
    return metadata_close(payload.get('market') if isinstance(payload, dict) else None, ticker)


def plan_recovery(snapshot: dict[str, Any], client: Any, *,
                  cache: dict[str, float] | None = None,
                  log: Callable[..., None] = emit,
                  sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, Any], dict[str, float]]:
    """Two-phase plan: no mutation of the supplied ledger, even on partial failure."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('markets'), dict):
        raise RecoveryPending('', 'Invalid legacy ledger; restore it rather than reset it')
    prepared = copy.deepcopy(snapshot)
    changes: dict[str, float] = {}
    cache = {} if cache is None else cache
    for ticker, record in snapshot['markets'].items():
        if not isinstance(ticker, str) or not ticker or not isinstance(record, dict):
            raise RecoveryPending(str(ticker), 'Invalid legacy market record')
        # Match the existing runner's ownership/cutover gate exactly.
        if not (record.get('entry_intents') or record.get('orders') or record.get('buys')):
            continue
        existing = record.get('close_timestamp')
        close = numeric_close(existing)
        if close is None:
            try:
                if ticker not in cache:
                    cache[ticker] = fetch_close(client, ticker)
                    sleep(0.15)  # Bound metadata request rate; no entry/exit workers run.
                close = cache[ticker]
                if numeric_close(close) is None:
                    raise RecoveryPending(ticker, 'Invalid recovered timestamp')
            except RecoveryPending:
                raise
            except Exception as error:
                delay = numeric_close(getattr(error, 'retry_after', None)) or 30
                raise RecoveryPending(ticker,
                    'Metadata lookup pending: ' + type(error).__name__ +
                    ' status=' + str(getattr(error, 'status_code', None)), max(30, delay)) from error
            log('TWO_RULE_LEGACY_CLOSE_RESOLVED', ticker=ticker, close_timestamp=close,
                source='exchange_market_metadata', settlement_or_fill_assertion=False)
        # Normalize numeric strings too; original runner compares to float time.
        if type(existing) not in (int, float) or existing != close:
            prepared['markets'][ticker]['close_timestamp'] = close
            changes[ticker] = close
    return prepared, changes


def commit_recovery(path: Path, original: bytes, prepared: dict[str, Any],
                    changes: dict[str, float]) -> Path | None:
    """Caller holds the same volume lock as both bots. Update metadata only."""
    path = Path(path)
    before = json.loads(original)
    expected = copy.deepcopy(before)
    for ticker, close in changes.items():
        if numeric_close(close) is None:
            raise RecoveryPending(ticker, 'Invalid planned close time')
        expected['markets'][ticker]['close_timestamp'] = close
    if expected != prepared:
        raise RecoveryPending('', 'Recovery attempted a non-close-time ledger change')
    if path.read_bytes() != original:
        raise RecoveryPending('', 'Ledger changed during recovery; no overwrite permitted')
    if not changes:
        return None
    digest = hashlib.sha256(original).hexdigest()
    backup = path.with_name(path.name + '.before-close-recovery-' + digest + '.bak')
    try:
        with backup.open('xb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if backup.read_bytes() != original:
            raise RecoveryPending('', 'Existing backup does not match the original ledger')
    with backup.open('rb') as stream:
        os.fsync(stream.fileno())  # Also flush a verified backup left by an interrupted run.
    directory = os.open(path.parent, os.O_RDONLY)
    temporary = path.with_name(path.name + '.close-recovery.tmp')
    try:
        os.fsync(directory)  # Backup is durable before replacement.
        encoded = (json.dumps(prepared, indent=2, allow_nan=False) + '\n').encode()
        with temporary.open('wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise RecoveryPending('', 'Ledger changed before replacement; no overwrite permitted')
        os.replace(temporary, path)
        os.fsync(directory)
    finally:
        os.close(directory)
        temporary.unlink(missing_ok=True)
    return backup


def recover_once(legacy: Any, client: Any, *, cache: dict[str, float],
                 log: Callable[..., None] = emit,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    legacy.validate_storage()  # Keep existing volume and spending-ledger validation.
    path = legacy.STATE
    original = path.read_bytes()
    snapshot = legacy.load_state()
    if json.loads(original) != snapshot:
        raise RecoveryPending('', 'Ledger changed while being loaded')
    prepared, changes = plan_recovery(snapshot, client, cache=cache, log=log, sleep=sleep)
    backup = commit_recovery(path, original, prepared, changes)
    verified = legacy.load_state()
    if verified != prepared:
        raise RecoveryPending('', 'Post-write ledger verification failed')
    owned = [r for r in verified['markets'].values()
             if r.get('entry_intents') or r.get('orders') or r.get('buys')]
    return {'repaired_records': len(changes), 'legacy_records': len(owned),
            'backup_created': backup is not None,
            'state_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'order_requests_sent': 0, 'spending_ledger_reset': False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-bot', action='store_true')
    args = parser.parse_args()
    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    import fcntl
    import bot as legacy
    from kalshi import KalshiClient
    # This client is used only for public metadata GETs, never portfolio APIs.
    client = KalshiClient(timeout=10)
    cache: dict[str, float] = {}
    try:
        while True:
            try:
                legacy.validate_storage()
                with legacy.STATE.with_suffix('.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = recover_once(legacy, client, cache=cache)
                emit('TWO_RULE_LEGACY_RECOVERY_OK', **result)
                break
            except Exception as error:
                delay = error.retry_after if isinstance(error, RecoveryPending) else 30
                emit('TWO_RULE_LEGACY_RECOVERY_WAIT',
                     ticker=error.ticker if isinstance(error, RecoveryPending) else '',
                     reason=str(error) if isinstance(error, RecoveryPending) else type(error).__name__,
                     retry_seconds=delay, trading_workers_started=False)
                if not args.start_bot:
                    raise SystemExit(1) from error
                time.sleep(delay)
        if args.start_bot:
            # Exec replaces the migration process; the unchanged runner re-acquires
            # the shared lock and still requires TRADING_ENABLED=true itself.
            target = Path(__file__).with_name('two_rule_bot.py')
            emit('TWO_RULE_STARTUP_HANDOFF', runner=target.name,
                 trading_enabled=os.getenv('TRADING_ENABLED', 'false').lower() == 'true')
            os.execv(sys.executable, [sys.executable, str(target), '--live'])
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    main()
