# Manual buys: exit at the fill price plus 5 cents

`manual_exit_runner.py` is a separate startup for manual exits in the existing
repository. It never starts `bot.main()`, opens automatic entries, cancels user
orders, or changes the original entry and automatic-exit journals.

**Prepared code, not activated or exchange-tested.** The runner defaults to
read-only observation. A human operator must deploy and enable live trading.

## Behavior

- A manual YES fill at 42c gets a 47c sell target. A NO fill at 42c also gets a
  47c NO target (53c on the API's YES price axis).
- The increment is **5c per contract before fees**, not 5c total per order.
  Ten contracts sold 5c higher produce 50c gross; fees reduce that amount.
- Each partial fill retains its own target. Fills at 40c and 42c are managed at
  45c and 47c respectively. Later fills, improved sale prices, manual reductions,
  and restarts do not lower the target on remaining contracts.
- The runner monitors bids and sends price-limited, reduce-only IOC exits at the
  target or better. **It must remain running. It does not post a standing sell
  after entry.** A brief price move can be missed, or an exit can partially fill.
- Kalshi V2 supports reduce-only orders with IOC, not GTC. This implementation
  retains that protection and does not silently fall back to unprotected orders.
- Targets are rounded upward to the exchange's valid price grid. A 96c fill
  needs 101c to gain 5c, which is unavailable: it is logged as unreachable and
  is not automatically lowered to 99c.
- By default, only new primary-account limit-order exposure in `KXBTC15M` and
  `KXETH15M` is eligible. Existing exposure from before this journal's first
  activation remains excluded. Other series and subaccounts are excluded.
- Old bot order IDs and original targets are read from the existing journals.
  Missing or inconsistent ownership data, unresolved acknowledgements and
  conflicting resting orders pause the affected market. User orders are not
  cancelled to make room for the bot's exit.

The API data does not prove human origin. No other API trading program should
create orders in the managed series. The runner takes the same local process
lock as `bot.py`; this prevents both from running on the same state volume, but
cannot lock out a separate machine or another API client. Exchange positions
are netted, so logical lot tracking is not physical custody isolation.

## Defaults and configuration check

The runner's defaults are independent of the old bot's profit constants:

```dotenv
TRADING_ENABLED=false
MANUAL_EXITS_ENABLED=false
MANUAL_EXITS_DRY_RUN=true
MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=false
MANUAL_EXIT_MODE=monitored_reduce_only
MANUAL_EXIT_INCREMENT_CENTS=5
MANUAL_EXIT_SERIES=KXBTC15M,KXETH15M
MANUAL_EXIT_POLL_SECONDS=1
STATE_PATH=/data/state.json
```

Validate without credentials or network calls:

```bash
python manual_exit_runner.py --check-config
```

The runner requires both existing ownership files, `state.json` and
`state_take_profit.json`, even for a read-only pass. `AUTOMATIC_EXIT_STATE_PATH`
can override the second path. Do not replace either with an empty file to bypass
recovery checks. A genuine new installation needs its own reviewed baseline.

Preview requires privately configured Kalshi credentials, but the client blocks
every write regardless of the account key's permissions:

```bash
python manual_exit_runner.py --once
python manual_exit_runner.py
```

Without `--live`, these commands remain read-only even if live environment
variables were accidentally left enabled. The existing automatic bot's
`TRADING_ENABLED` must be false; the exits-only runner rejects true.

Default preview journal: `/data/state_manual_5c_preview.json`.
Default live journal: `/data/state_manual_5c_exits.json`.
Both derive their directory and prefix from `STATE_PATH`. Live and preview have
separate activation times, so preview trades are not automatically adopted when
live mode is first enabled. Once created, retain the live journal across restarts.
Do not delete a journal to work around an unresolved order acknowledgement.

## Railway operator handoff

The inspected service is `impartial-prosperity / production / v2`. It currently
tracks `fix/final2-96-10-30c` and starts `python bot.py`. Those production settings
were not modified by this change. Credentials already exist in Railway and
must not be copied into the repository or chat.

For an operator-run preview, deploy the reviewed revision with the existing
`/data` volume and use `python manual_exit_runner.py` as the start command. Set
the configuration above explicitly; the earlier hybrid `.env.example` has a
different series default. Keep the manual increment at 5 cents.

After reviewing the preview and accepting monitored execution, an operator can
use `python manual_exit_runner.py --live` with all three manual gates set:
`MANUAL_EXITS_ENABLED=true`, `MANUAL_EXITS_DRY_RUN=false`, and
`MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=true`. `TRADING_ENABLED` stays false because
it controls the old automatic-entry strategy. The live runner creates a new
activation time only when its live journal does not already exist.

The startup log reports the mode, increment, series, dry-run state and absence
of automatic entries. `MANUAL_FILL_ADOPTED` reports the unrounded target;
`MANUAL_DRY_RUN_TARGET` reports what would be submitted at an available target;
`TP_SUBMITTED`/`TP_FILL` prefixed with `MANUAL_` distinguish actual submissions
from confirmed fills. A paused market is not reported as sold. Close stops new
submissions and preserves unresolved intent records for later inspection.

## Verification

```bash
python tools/run_offline_tests.py -q test_manual_five_cent_exits.py test_manual_limit_exits.py test_hybrid_runtime.py test_co_trading.py
```

The tests block TCP connections and use an exchange simulator. They cover YES/NO,
fractional quantities, multiple fills, manual reductions, improved partial
sales, duplicate histories, lost acknowledgements, restarts, target bounds,
price grids, old bot ownership, conflicting resting sells, closed markets,
preview/live separation and the client's prohibition on other writes.

API reference: https://docs.kalshi.com/api-reference/orders/create-order-v2
