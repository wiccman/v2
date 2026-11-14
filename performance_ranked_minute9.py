"""Performance-ranked fallback for the individual-coin minute-nine runner.

Rank highest signed Coinbase USD return since this 15-minute window opened.
This ranks coins, not Kalshi probabilities, and does not trade Crypto Leader.
A priced-out leader falls through to the next ranked individual coin. All
existing price, timing, ownership, one-attempt, TP, stop and budget rules remain.
Coins with unavailable data are skipped; rank the remaining valid observations.
No valid observations blocks new entries, not exits.
Run explicitly with: python performance_ranked_minute9.py --live
"""
from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Iterable

import requests
import single_coin_minute9 as core

PRODUCTS = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD",
            "KXSOL15M": "SOL-USD", "KXXRP15M": "XRP-USD",
            "KXHYPE15M": "HYPE-USD"}
BASE = "https://api.exchange.coinbase.com"
MAX_AGE = 15.0
MAX_SKEW = 10.0
REFRESH_SECONDS = 4.0
_CORE_RUN_PASS = core.run_pass


class PerformanceUnavailable(ValueError):
    """No defensible ranking or observation; existing exits must continue."""


@dataclass(frozen=True)
class Observation:
    series: str
    window_start: int
    opening: D
    price: D
    observed_at: float

    @property
    def return_pct(self) -> D:
        return (self.price / self.opening - 1) * 100


def ranked_observations(rows, series_list, window_start, now, *, allow_partial=False):
    """Rank valid same-window data; explicitly allow missing coins when requested."""
    names = tuple(series_list)
    if (type(window_start) is not int or window_start % 900
            or not math.isfinite(now) or not window_start <= now < window_start + 900
            or not names or len(set(names)) != len(names)
            or any(s not in PRODUCTS for s in names)):
        raise PerformanceUnavailable("invalid_window_or_series")
    observed = [r.series for r in rows]
    if (len(set(observed)) != len(observed) or not set(observed).issubset(names)
            or (not allow_partial and set(observed) != set(names))):
        raise PerformanceUnavailable("incomplete_coin_snapshot")
    if not rows:
        raise PerformanceUnavailable("no_valid_coin_observations")
    for r in rows:
        if (r.window_start != window_start or not math.isfinite(r.observed_at)
                or not r.opening.is_finite() or not r.price.is_finite()
                or r.opening <= 0 or r.price <= 0
                or not window_start <= r.observed_at < window_start + 900
                or not -1 <= now - r.observed_at <= MAX_AGE):
            raise PerformanceUnavailable("stale_or_invalid_coin_observation")
    if max(r.observed_at for r in rows) - min(r.observed_at for r in rows) > MAX_SKEW:
        raise PerformanceUnavailable("coin_observations_too_far_apart")
    priority = {name: i for i, name in enumerate(names)}
    return sorted(rows, key=lambda r: (-r.return_pct, priority[r.series]))


class CoinbasePerformance:
    """Read-only public feed, separate from the authenticated trading client."""
    def __init__(self, *, clock=time.time, session_factory=requests.Session, emit=None):
        self.clock, self.session_factory = clock, session_factory
        self.emit = emit or core.event
        self.opens = {}
        self.cached = None
        self.last_attempt = float("-inf")

    def _one(self, series, start):
        product = PRODUCTS[series]
        opening = self.opens.get((start, series))
        with self.session_factory() as session:
            # Never reuse the Kalshi session or attach brokerage credentials.
            if opening is None:
                iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
                response = session.get(f"{BASE}/products/{product}/candles",
                    params={"granularity": 60, "start": iso(start), "end": iso(start + 60)},
                    timeout=(1, 2))
                response.raise_for_status()
                candles = response.json()
                if not isinstance(candles, list):
                    raise PerformanceUnavailable("invalid_opening_candles")
                matching = [r for r in candles if isinstance(r, list) and len(r) >= 5
                            and r[0] == start]
                if len(matching) != 1:
                    raise PerformanceUnavailable("opening_candle_missing_or_ambiguous")
                opening = D(str(matching[0][3]))
            response = session.get(f"{BASE}/products/{product}/ticker", timeout=(1, 2))
            response.raise_for_status()
            tick = response.json()
            return Observation(series, start, opening, D(str(tick["price"])),
                               core.parse_time(tick["time"]))

    def snapshot(self, series_list, start):
        names, now = tuple(series_list), self.clock()
        if self.cached is not None and self.cached[0:2] == (start, names):
            if 0 <= now - self.cached[2] < REFRESH_SECONDS:
                return ranked_observations(self.cached[3], names, start, now,
                                           allow_partial=True)
        if now - self.last_attempt < REFRESH_SECONDS:
            raise PerformanceUnavailable("performance_feed_retry_cooldown")
        self.last_attempt = now
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            pending = [(series, pool.submit(self._one, series, start)) for series in names]
        finished = self.clock()
        rows = []
        for series, future in pending:
            try:
                row = future.result()
                # Validate independently so a missing or stale coin cannot discard
                # the rest. Never substitute a later candle or a different window.
                ranked_observations([row], (series,), start, finished)
            except Exception as error:
                self.emit("M9_PERFORMANCE_COIN_SKIPPED", series=series,
                          window_start_ms=start * 1000, **error_fields(error))
                continue
            rows.append(row)
        ranked = ranked_observations(rows, names, start, finished, allow_partial=True)
        self.opens = {(start, r.series): r.opening for r in rows}
        self.cached = (start, names, finished, rows)
        return ranked


def error_fields(error):
    """Expose diagnostic codes, not response bodies, credentials or headers."""
    fields = {"error_type": type(error).__name__}
    status = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    if type(status) is int and 100 <= status <= 599:
        fields["http_status"] = status
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        fields["exchange_error_code"] = code
    if isinstance(error, PerformanceUnavailable):
        fields["reason"] = str(error)
    return fields


class QueueDiagnostics:
    """Transparent client proxy: add quote diagnostics, never change orders."""
    def __init__(self, client, *, clock, emit):
        self.client, self.clock, self.emit = client, clock, emit
        self.ranks = {}
        self.start = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def markets(self, **kwargs):
        rows = self.client.markets(**kwargs)
        series = kwargs.get("series_ticker")
        for row in rows:
            try:
                close = core.parse_time(row["close_time"])
                if self.start is None or close != self.start + 900:
                    continue
                plan = core.plan_single_coin(row, self.clock(), series)
                self.emit("M9_QUEUE_ELIGIBLE" if plan else "M9_QUEUE_SKIPPED",
                    series=series, rank=self.ranks.get(series), ticker=row.get("ticker"),
                    yes_ask=row.get("yes_ask_dollars"), no_ask=row.get("no_ask_dollars"),
                    reason="eligible_price" if plan else "price_status_or_time_ineligible")
            except (ValueError, KeyError, TypeError):
                self.emit("M9_QUEUE_SKIPPED", series=series, reason="invalid_market_fields")
        return rows


def run_ranked_pass(client, journal, series_list, base_budget, max_contracts,
                    live, now, max_entries_per_window=1, *, provider,
                    clock=time.time, emit=None):
    emit = emit or core.event
    names = tuple(series_list)
    proxy = QueueDiagnostics(client, clock=clock, emit=emit)

    def candidates() -> Iterable[str]:
        # Core reconciles exits before it first iterates this generator.
        checked = clock()
        start = int(checked // 900) * 900
        if not core.ENTRY_START_SECONDS <= checked - start < core.ENTRY_END_SECONDS:
            return
        if core.window_entry_count(journal, start * 1000) >= max_entries_per_window:
            return
        try:
            rows = provider.snapshot(names, start)
            rows = ranked_observations(rows, names, start, clock(), allow_partial=True)
        except Exception as error:
            emit("M9_PERFORMANCE_UNAVAILABLE", **error_fields(error))
            return
        if not core.ENTRY_START_SECONDS <= clock() - start < core.ENTRY_END_SECONDS:
            emit("M9_PERFORMANCE_WINDOW_EXPIRED", window_start_ms=start * 1000)
            return
        proxy.start = start
        proxy.ranks = {r.series: i + 1 for i, r in enumerate(rows)}
        emit("M9_PERFORMANCE_QUEUE", window_start_ms=start * 1000,
             source="coinbase_usd", metric="signed_return_from_window_open",
             ranking_scope="available_coin_data",
             excluded_series=[s for s in names if s not in proxy.ranks],
             queue=[{"rank": i + 1, "series": r.series,
                     "return_pct": str(r.return_pct), "observed_at": r.observed_at}
                    for i, r in enumerate(rows)])
        for row in rows:
            if not core.ENTRY_START_SECONDS <= clock() - start < core.ENTRY_END_SECONDS:
                return
            yield row.series

    try:
        return _CORE_RUN_PASS(proxy, journal, candidates(), base_budget, max_contracts,
                              live, now, max_entries_per_window)
    except Exception as error:
        # Original runner logs only the exception type; preserve useful safe codes.
        emit("M9_RANKED_PASS_ERROR", **error_fields(error))
        raise


def main(argv=None):
    """Reuse the original gates, volume lock, journal, credentials and settings."""
    provider = CoinbasePerformance()
    previous = core.run_pass

    def run(*args, **kwargs):
        return run_ranked_pass(*args, **kwargs, provider=provider)

    # Scoped dependency replacement only in this explicit executable entrypoint.
    # Importing this module never changes the original runner or places trades.
    core.run_pass = run
    try:
        return core.main(argv)
    finally:
        core.run_pass = previous


if __name__ == "__main__":
    main()
