"""Opt-in composition root for the existing bot and manual exit manager.

With MANUAL_EXITS_ENABLED=false this delegates to the unmodified bot.main().
Automatic strategy constants, entry sizing, budgets and targets are untouched.
"""
from __future__ import annotations

import functools
import os
import threading
from pathlib import Path


def flag(environ, name, default="false"):
    value = environ.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def configure(bot_module, environ=None):
    environ = os.environ if environ is None else environ
    if not flag(environ, "MANUAL_EXITS_ENABLED"):
        return False
    if environ.get("MANUAL_EXIT_MODE", "monitored_reduce_only") != "monitored_reduce_only":
        raise ValueError("Kalshi V2 rejects resting reduce-only sells; use monitored_reduce_only")
    if not flag(environ, "MANUAL_EXTERNAL_ORDERS_ARE_MANUAL"):
        raise ValueError("Confirm that new non-bot orders in the managed series are manual before enabling")
    if getattr(bot_module, "_hybrid_configured", False):
        raise RuntimeError("Hybrid runtime was already configured")
    from manual_limit_exits import ManualLimitExitWorker
    series = tuple(s.strip() for s in environ.get("MANUAL_EXIT_SERIES", "KXBTC15M").split(",") if s.strip())
    if not series or any(not s.isalnum() for s in series):
        raise ValueError("MANUAL_EXIT_SERIES must contain explicit series tickers")
    dry_run = flag(environ, "MANUAL_EXITS_DRY_RUN", "true")
    original_monitor = bot_module.TakeProfitMonitor
    original_entry = bot_module.funded_entry
    lock = threading.RLock()

    def monitor_factory(client, read_entries, path, **kwargs):
        manual_path = Path(bot_module.STATE).with_name(Path(bot_module.STATE).stem + "_manual_exits_v2.json")
        manual = ManualLimitExitWorker(client, manual_path, series=series, dry_run=dry_run)
        return original_monitor(client, read_entries, path, manual_exits=manual,
                                execution_lock=lock, **kwargs)

    @functools.wraps(original_entry)
    def serialized_entry(*args, **kwargs):
        # The existing entry function still makes every strategy decision. The
        # lock prevents bot POSTs racing either exit worker's attribution pass.
        with lock:
            return original_entry(*args, **kwargs)

    bot_module.TakeProfitMonitor = monitor_factory
    bot_module.funded_entry = serialized_entry
    bot_module._hybrid_configured = True
    return True


def main():
    import bot  # Loads the existing .env and production safeguards first.
    if configure(bot):
        print("MANUAL_EXIT_MODE=monitored_reduce_only; gross_goal=$0.25 per original buy order; "
              "new non-bot limit fills only; dry-run=" + os.getenv("MANUAL_EXITS_DRY_RUN", "true"), flush=True)
    bot.main()


if __name__ == "__main__":
    main()
