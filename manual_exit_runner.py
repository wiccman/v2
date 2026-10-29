"""Manual buys only: post a resting sell at fill price + 5 cents.

Defaults to read-only observation. Production activation is an operator action.
Existing automatic entry/exit journals are read for ownership, never modified.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from kalshi import KalshiClient, KalshiAPIError
from manual_limit_exits import (ManualLimitExitWorker, ManualExitBlocked, number,
                               position, primary_orders, quantity, failure_context)
from request_coordinator import RequestDeferred
from resting_manual_exits import RestingExitOnlyClient, RestingManualExitWorker


def flag(environ, name, default="false"):
    value = environ.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    entry_state: Path
    automatic_exit_state: Path
    manual_state: Path
    series: tuple[str, ...]
    increment: D
    poll: float
    dry_run: bool
    execution_mode: str

    @classmethod
    def read(cls, env, *, live=False):
        entry = Path(env.get("STATE_PATH", "/data/state.json"))
        automatic = Path(env.get("AUTOMATIC_EXIT_STATE_PATH", str(entry.with_name(entry.stem + "_take_profit.json"))))
        mode = env.get("MANUAL_EXIT_MODE", "resting_limit")
        if mode not in {"resting_limit", "monitored_reduce_only"}:
            raise ValueError("MANUAL_EXIT_MODE must be resting_limit or monitored_reduce_only")
        prefix = "_manual_5c_resting" if mode == "resting_limit" else "_manual_5c"
        suffix = prefix + ("_exits.json" if live else "_preview.json")
        manual = Path(env.get("MANUAL_EXIT_STATE_PATH", str(entry.with_name(entry.stem + suffix))))
        if len({p.resolve() for p in (entry, automatic, manual)}) != 3:
            raise ValueError("Manual, automatic entry and automatic exit journals must be distinct")
        if flag(env, "TRADING_ENABLED"):
            raise ValueError("Use TRADING_ENABLED=false for the exits-only runner; automatic entries must stay off")
        if env.get("KALSHI_ENV", "production") != "production":
            raise ValueError("This runner uses production API URLs; use dry-run for observation")
        increment = number(env.get("MANUAL_EXIT_INCREMENT_CENTS", "5"), "increment cents") / 100
        if not D(0) < increment < 1 or increment.quantize(D(".0001")) != increment:
            raise ValueError("Increment must be positive, below 100 cents, with at most 0.01-cent precision")
        series = tuple(s.strip() for s in env.get("MANUAL_EXIT_SERIES", "KXBTC15M,KXETH15M").split(",") if s.strip())
        if not series or len(series) != len(set(series)) or any(not s.isalnum() for s in series):
            raise ValueError("MANUAL_EXIT_SERIES requires distinct explicit series tickers")
        poll = number(env.get("MANUAL_EXIT_POLL_SECONDS", "1"), "poll seconds")
        if not D(1) <= poll <= D(60):
            raise ValueError("Poll interval must be between 1 and 60 seconds")
        if live and (not flag(env, "MANUAL_EXITS_ENABLED") or flag(env, "MANUAL_EXITS_DRY_RUN", "true")
                     or not flag(env, "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL")):
            raise ValueError("Live mode requires MANUAL_EXITS_ENABLED=true, MANUAL_EXITS_DRY_RUN=false, "
                             "and MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=true")
        return cls(entry, automatic, manual, series, increment, float(poll), not live, mode)


class ExitOnlyClient(KalshiClient):
    """Deny entries, transfers, cancellations and all writes during dry-run."""
    def __init__(self, *args, dry_run=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.dry_run = dry_run
        self.base = "https://external-api.kalshi.com/trade-api/v2"

    def request(self, method, path, params=None, body=None, auth=False):
        if method.upper() != "GET":
            permitted_exit = (not self.dry_run and method.upper() == "POST"
                              and path == "/portfolio/events/orders" and isinstance(body, dict)
                              and body.get("reduce_only") is True
                              and body.get("time_in_force") == "immediate_or_cancel")
            if not permitted_exit:
                raise RuntimeError("Exit-only client refused an unauthorized write")
        return super().request(method, path, params=params, body=body, auth=auth)


def read_journal(path):
    # A missing old ledger must not turn known automated orders into manual ones.
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("markets"), dict):
        raise ManualExitBlocked("Ownership journal must contain a markets object")
    if any(not isinstance(v, dict) for v in data["markets"].values()):
        raise ManualExitBlocked("Ownership journal contains an invalid market record")
    return data


def automatic_entries(record, ledger, orders):
    """Recover known entry IDs; retain their original side/target for replay."""
    by_client = {o.get("client_order_id"): o for o in orders if o.get("client_order_id")}
    entries = {}
    recovered = dict(ledger.get("entry_order_ids", {}))
    for intent in record.get("entry_intents", []):
        cid = intent.get("client_id")
        oid = intent.get("order_id") or recovered.get(cid)
        if not oid and cid in by_client:
            oid = by_client[cid]["order_id"]
            recovered[cid] = oid
        if not oid:
            if not intent.get("entry_closed"):
                raise ManualExitBlocked("Unresolved automatic buy; manual classification paused")
            continue
        side, target = intent.get("side"), intent.get("exit_target")
        if side not in {"YES", "NO"} or target is None:
            raise ManualExitBlocked("Automatic entry is missing its original side/target")
        target = number(target, "automatic target")
        if not 0 < target <= 1:
            raise ManualExitBlocked("Invalid automatic target")
        entry = {"side": side, "target": str(target)}
        if oid in entries and entries[oid] != entry:
            raise ManualExitBlocked("Conflicting automatic entry ownership")
        entries[oid] = entry
    return entries, {**ledger, "entry_order_ids": recovered}


def verify_exit_fills(exits, fills):
    counts, seen = defaultdict(lambda: D(0)), {}
    for fill in fills:
        fid = fill.get("fill_id") or fill.get("trade_id")
        if not fid:
            raise ManualExitBlocked("Fill ID missing")
        if fid in seen:
            if seen[fid] != fill:
                raise ManualExitBlocked("Conflicting duplicate fill")
            continue
        seen[fid] = fill
        counts[fill.get("order_id")] += quantity(fill.get("count_fp", fill.get("count")))
    for oid, order in exits.items():
        if "filled" not in order or counts[oid] != quantity(order["filled"]):
            raise ManualExitBlocked("Exit order/fill history has not converged")


class ManualExitService:
    CLOSED_RETRY_SECONDS = 30.0

    def __init__(self, client, settings, *, clock=None, emit=None):
        self.client, self.settings = client, settings
        self.emit = emit or (lambda event, **data: print(json.dumps({
            "event": event, "time_utc": datetime.now(timezone.utc).isoformat(), **data}), flush=True))
        # Validate both journals before creating the new manual activation time.
        self.read_entries()
        read_journal(settings.automatic_exit_state)
        runner_mode = "dry_run" if settings.dry_run else "live"
        if settings.manual_state.exists():
            saved = read_journal(settings.manual_state)
            if saved.get("manual_runner_mode") != runner_mode:
                raise ManualExitBlocked("Use separate journals for live and dry-run activation")
            if saved.get("execution_mode", "monitored_reduce_only") != settings.execution_mode:
                raise ManualExitBlocked("Use separate journals for resting and monitored execution")
        options = {"clock": clock} if clock is not None else {}
        worker = RestingManualExitWorker if settings.execution_mode == "resting_limit" else ManualLimitExitWorker
        self.worker = worker(client, settings.manual_state,
            price_increment=settings.increment, series=settings.series, dry_run=settings.dry_run,
            emit=self.emit, **options)
        self.worker.bind(self.read_entries)
        self.worker.state["manual_runner_mode"] = runner_mode
        self.worker.state["execution_mode"] = settings.execution_mode
        self.worker.save()
        # Scheduling hints never replace order/inventory reconciliation. Keep
        # unresolved expired orders in the journal, but take their cleanup out
        # of the path that detects a fresh manual fill.
        self._market_closes = {}
        self._closed_markets = set()
        self._closed_attempted = {}
        self._closed_retry_after = {}
        self._closed_confirm = set()

    def read_entries(self):
        return read_journal(self.settings.entry_state)

    def close_hint(self, ticker):
        if ticker in self._market_closes:
            return self._market_closes[ticker]
        # Existing resting intents provide a startup scheduling hint before a
        # market lookup. They are not proof of an order's terminal state.
        intents = self.worker.state["markets"].get(ticker, {}).get("resting_intents", {})
        if not isinstance(intents, dict) or any(not isinstance(i, dict) for i in intents.values()):
            return None
        try:
            return float(max(number(i["expires_at"], "expiry") for i in intents.values())) if intents else None
        except (KeyError, TypeError, ValueError):
            return None  # The normal reconciliation path will validate it.

    def scheduled_markets(self, tickers):
        now = self.worker.clock()
        active, expired = [], []
        for ticker in tickers:
            if self.worker.ledger(ticker).get("window_closed"):
                continue
            close = self.close_hint(ticker)
            if ticker in self._closed_markets or (close is not None and now >= close):
                expired.append(ticker)
            else:
                active.append(ticker)
        # A ticker with no prior expiry hint is commonly a newly filled market.
        # Always service it before any historical cleanup, on restart too.
        active.sort(key=lambda ticker: (bool(self.worker.ledger(ticker)),
                                        self.close_hint(ticker) is not None, ticker))
        due = [ticker for ticker in expired if now >= self._closed_retry_after.get(ticker, 0)]
        due.sort(key=lambda ticker: (ticker not in self._closed_confirm,
                                    self._closed_attempted.get(ticker, float("-inf")), ticker))
        # One expired market per pass bounds the cost even with a large backlog.
        # Least-recently-attempted ordering prevents one broken record starving
        # all other cleanup. Deferred records still make the pass unhealthy.
        return active + due[:1], len(expired) > len(due[:1])

    def reconcile_closed(self, ticker, close):
        self._closed_markets.add(ticker)
        self._closed_attempted[ticker] = self.worker.clock()
        self._closed_confirm.discard(ticker)
        def progress():
            # Merely moving a cancellation retry timer is not proof of progress
            # and must not keep one unconfirmed order at the front of the queue.
            return json.dumps({cid: {key: value for key, value in intent.items()
                                    if key != "cancel_retry_at"}
                               for cid, intent in self.worker.ledger(ticker).get("resting_intents", {}).items()},
                              sort_keys=True)
        before = progress()
        closed = self.worker.window_closed(ticker, close)
        if self.settings.execution_mode != "resting_limit" or closed:
            self._closed_retry_after.pop(ticker, None)
            return True
        after = progress()
        if after != before:
            # A cancellation/status change merits one prompt confirmation read.
            self._closed_confirm.add(ticker)
            self._closed_retry_after.pop(ticker, None)
        else:
            self._closed_retry_after[ticker] = self.worker.clock() + self.CLOSED_RETRY_SECONDS
        return False

    def run_once(self):
        # A fresh read detects another writer even if it bypasses the file lock.
        try:
            self.read_entries()
            read_journal(self.settings.automatic_exit_state)
            tickers, deferred_cleanup = self.scheduled_markets(self.worker.discover())
        except Exception:
            for ticker in self.worker.state["markets"]:
                self.suspend(ticker)
            raise
        ok = not deferred_cleanup
        closed_attempts = 0
        for ticker in tickers:
            if self.worker.ledger(ticker).get("window_closed"):
                continue
            hint = self.close_hint(ticker)
            historical = ticker in self._closed_markets or (hint is not None and self.worker.clock() >= hint)
            if historical:
                if closed_attempts:
                    ok = False
                    continue
                self._closed_attempted[ticker] = self.worker.clock()
            stage = "market"
            try:
                if ticker in self._closed_markets:
                    stage = "market_close"
                    closed_attempts += 1
                    ok = self.reconcile_closed(ticker, self._market_closes[ticker]) and ok
                    continue
                market = self.client.market(ticker)
                close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
                if close.tzinfo is None:
                    raise ManualExitBlocked("Market close requires a timezone")
                self._market_closes[ticker] = close.timestamp()
                if self.worker.clock() >= close.timestamp() or market.get("status") in {"closed", "settled", "finalized"}:
                    self._closed_markets.add(ticker)
                    if closed_attempts:
                        ok = False
                        continue
                    stage = "market_close"
                    closed_attempts += 1
                    ok = self.reconcile_closed(ticker, close.timestamp()) and ok
                    continue
                # Several partial buy fills can arrive together. Reconcile
                # after every action, reserving each accepted order's quantity
                # before posting another. Bound work so other markets get time.
                limit = 20 if self.settings.execution_mode == "resting_limit" else 1
                for _ in range(limit):
                    stage = "reconcile_orders"
                    if not self.worker.reconcile(ticker):
                        ok = False
                        self.suspend(ticker)
                        break
                    stage = "automatic_ownership"
                    record = self.read_entries()["markets"].get(ticker, {})
                    ledger = read_journal(self.settings.automatic_exit_state)["markets"].get(ticker, {})
                    if ledger.get("pending"):
                        raise ManualExitBlocked("Unresolved automatic exit; manual exits paused")
                    orders = primary_orders(self.client.all_orders(ticker), ticker)
                    entries, ownership = automatic_entries(record, ledger, orders)
                    stage = "read_inventory"
                    fills = self.client.all_fills(ticker)
                    held = position(self.client.positions(ticker), ticker)
                    exits = self.worker.combined_exits(ticker, ledger.get("exit_orders", {}))
                    stage = "verify_exit_fills"
                    verify_exit_fills(exits, fills)
                    stage = "observe_inventory"
                    self.worker.observe(ticker, record, ownership, fills, entries, exits, held)
                    stage = "submit_exit"
                    if not self.worker.maybe_submit(ticker, record, ownership):
                        break
            except Exception as error:
                ok = False
                if stage == "market_close" or (historical and stage == "market"):
                    self._closed_confirm.discard(ticker)
                    self._closed_retry_after[ticker] = self.worker.clock() + self.CLOSED_RETRY_SECONDS
                if isinstance(error, (KalshiAPIError, RequestDeferred)):
                    self.worker.transport.defer(self.worker.ledger(ticker), error)
                self.suspend(ticker)
                self.emit("MANUAL_EXIT_PAUSED", ticker=ticker, stage=stage, **failure_context(error))
        return ok

    def suspend(self, ticker):
        if isinstance(self.worker, RestingManualExitWorker):
            self.worker.suspend(ticker)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Operator opt-in; also requires all live environment gates")
    parser.add_argument("--once", action="store_true", help="Run one reconciliation pass")
    parser.add_argument("--check-config", action="store_true", help="Validate settings without credentials or API calls")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    settings = Settings.read(os.environ, live=args.live)
    print(json.dumps({"event": "MANUAL_EXIT_CONFIG", "mode": settings.execution_mode,
                      "increment_cents": str(settings.increment * 100), "series": settings.series,
                      "poll_seconds": settings.poll,
                      "dry_run": settings.dry_run, "automatic_entries": False}), flush=True)
    if args.check_config:
        return 0
    if os.getenv("RAILWAY_ENVIRONMENT_ID"):
        mount = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/missing-volume"))
        if not os.path.ismount(mount) or not all(p.resolve().is_relative_to(mount.resolve()) for p in
                (settings.entry_state, settings.automatic_exit_state, settings.manual_state)):
            raise RuntimeError("All journals must be on the persistent Railway volume")
    import fcntl  # The deployed worker runs on Linux.
    # Same lock as bot.py, so a local automatic-entry process cannot coexist.
    with settings.entry_state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client_class = RestingExitOnlyClient if settings.execution_mode == "resting_limit" else ExitOnlyClient
        client = client_class(os.getenv("KALSHI_API_KEY_ID", ""),
            os.getenv("KALSHI_PRIVATE_KEY_PATH", ""), os.getenv("KALSHI_PRIVATE_KEY_B64", ""),
            timeout=5, role="exit", dry_run=settings.dry_run)
        if not client.key_id or client.private_key is None:
            raise RuntimeError("Kalshi credentials are required; configure them privately in the hosting environment")
        service = ManualExitService(client, settings)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            try:
                ok = service.run_once()
            except Exception as error:
                ok = False
                service.emit("MANUAL_EXIT_PASS_FAILED", **failure_context(error))
            if args.once:
                return 0 if ok else 1
            stop.wait(settings.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
