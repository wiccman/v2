# Manual fills: immediately post a resting sell 5 cents higher

`manual_exit_runner.py` is the default Docker startup on this branch. Railway
uses that default unless an operator selects a start-command override. The
runner never imports the legacy bot or hybrid runtime, opens automatic
entries, or starts the old automatic-exit worker. Old entry and exit journals
are read only to identify and exclude bot-owned inventory.

**Prepared code, not activated or exchange-tested.** Default startup is read-only.
The production service is not changed by editing this branch.

## Requested behavior

- A manual YES buy fills at 42c: on detecting the fill, post a 47c YES limit
  sell for the filled quantity. A NO fill at 42c gets a 47c NO sell (a bid at
  53c on the API's YES price axis).
- The sell is submitted even when the current bid is below the target. There
  is no wait-for-price trigger. Kalshi holds the accepted limit order; it can
  fill while the bot is offline, subject to available matching orders.
- Use `good_till_canceled` with expiry at the market's close and
  `cancel_order_on_pause=true`. It remains until filled, canceled, or expired.
  It may fill immediately if its target is already marketable; post-only is off.
- Detection uses polling, with a one-second cadence by default.
  API latency, rate limits, reconciliation and other markets add delay. This is
  not an exchange-native bracket and does not guarantee instantaneous posting.
  Up to 20 actions per market are reconciled in each pass so multiple detected
  partial fills can receive their separate orders without waiting for price.
- Each fill keeps its actual cost plus **5c per contract before fees**. Ten
  contracts sold 5c higher yield 50c gross. Fills at 40c and 42c produce separate
  45c and 47c sell orders. Later fills and better sale prices do not lower targets.
- Targets round upward to the valid exchange grid. If entry plus 5c is not
  tradable (for example 96c + 5c), log it as unreachable and do not lower it.
- Only newly filled primary-account limit-buy exposure in `KXBTC15M` and
  `KXETH15M` is eligible by default. Add `KXWTI15M` explicitly for WTI 15-minute
  coverage. Fills before the series' activation, other series, subaccounts,
  and old bot-owned inventory are excluded.
  An order placed earlier can qualify if its fill occurs after activation.

## Adding WTI 15-minute coverage

Deploy this revision together with
`MANUAL_EXIT_SERIES=KXBTC15M,KXETH15M,KXWTI15M`. The exact `KXWTI15M-` prefix
excludes daily/hourly WTI and unrelated markets. YES and NO fills use the same
per-fill +5c target and fractional quantities as BTC/ETH.

The worker reads each market's `price_ranges`. For WTI's currently published
tapered grid, a 9.9c fill targets 15c (14.9c is off-grid), while a 94.9c fill can
target 99.9c. Entry plus 5c at or above $1 is unreachable and is not silently
capped. These are gross price increments before fees.

Existing schema-2 journals upgrade atomically to schema 3. Existing activation
times, fills, resting-order IDs and quantity reservations are retained. Appended
series receive their own first-enable timestamp; older WTI fills are not adopted.
The configuration can append series, but cannot remove, replace or reorder the
saved list. Restarts preserve every series' cutoff. Keep the same journal path.

Older runners reject schema 3 instead of ignoring its cutoffs. A rollback must
use schema-3-compatible code and preserve the expanded series list; do not reset
or restore an older journal to force an old runner to start.

## Compatibility with the deployed latency fix

This WTI revision is based on deployed commit `69de776` and preserves its
`terminal-first-v1` runner, pooled HTTP transport, cadence-based polling and
latency telemetry. New positions and previously confirmed live markets precede
historical cleanup. Venue-confirmed closed markets defer repeated maintenance;
settled/finalized markets past their close time are retired from the polling
loop while their original order history and reservations remain recorded.

The WTI change updates journal scope and activation cutoffs only. It retains
the deployed fill/order/position checks, submission logic and timing behavior.
Neither an HTTP 404 nor an elapsed expiry is treated as proof of a sale or
cancellation. Polling and API latency still preclude an instantaneous-posting
guarantee.

## Resting-order limitation

Kalshi V2 rejects `reduce_only=true` with GTC. These standing exits therefore
use `reduce_only=false`. The application verifies ownership and size before
posting, but **cannot guarantee that a resting order only reduces the position**.
If another sale or opposing buy consumes the position, the outstanding order
could instead create opposite exposure. Local process locks do not lock out
manual trading or another API client, and no local check removes this race.

**Cancel the bot's resting sell before manually closing the same position.**
Keep the worker online to detect later fills and inventory changes. Stopping it
leaves already accepted resting orders on the exchange; it cannot resize or
cancel them while offline. No other trading bot should operate in these series.
The API does not prove human origin; new external non-bot limit buys are treated
as manual only under that operating assumption.

The worker journals each intent before posting, recovers acknowledgements by
client ID, reserves outstanding quantities, and waits for terminal cancellation
before reusing size. Partial exit fills keep the remaining reservation. A manual
reduction triggers cancellation of oversized owned exits and replacement only
for the verified remainder. Conflicting user orders or inconsistent ownership
pause new exits and trigger best-effort cancellation of the worker's own exits.
User orders are never canceled. Network failure can prevent these cancellations.

A user cancellation or unexplained exchange cancellation suppresses further
orders for that same fill instead of undoing the cancellation. New buy fills
remain eligible. Uncertain submissions/cancellations stay blocked across restarts;
a missing order or 404 is not proof that an order never existed. Outstanding
order records retain any ambiguity even when a venue-finalized market is
retired from polling; market retirement does not assert a fill or cancellation.

## Configuration and preview

The sample `.env.example` contains only manual-runner settings. Legacy buy-rule
variables cannot select or launch the old strategy. `TRADING_ENABLED=true` is
rejected by this runner.

```dotenv
KALSHI_ENV=production
TRADING_ENABLED=false
MANUAL_EXITS_ENABLED=false
MANUAL_EXITS_DRY_RUN=true
MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=false
MANUAL_EXIT_MODE=resting_limit
MANUAL_EXIT_INCREMENT_CENTS=5
MANUAL_EXIT_SERIES=KXBTC15M,KXETH15M
MANUAL_EXIT_POLL_SECONDS=1
STATE_PATH=/data/state.json
AUTOMATIC_EXIT_STATE_PATH=/data/state_take_profit.json
```

Validate without credentials or network calls:

```bash
python manual_exit_runner.py --check-config
```

The runner requires the existing `state.json` and `state_take_profit.json`
ownership files even for preview. Do not replace missing files with empty ones
just to bypass recovery checks. Configure existing Kalshi credentials privately
in Railway; never copy them into a commit or chat.

```bash
python manual_exit_runner.py --once
python manual_exit_runner.py
```

Without `--live`, these commands are read-only regardless of environment gates.
Preview reports `MANUAL_DRY_RUN_RESTING_ORDER` as soon as it can verify the fill,
even below the target price. It never posts or cancels an exchange order.

Default journal files:

- Preview: `/data/state_manual_5c_resting_preview.json`
- Live: `/data/state_manual_5c_resting_exits.json`

The directory and prefix derive from `STATE_PATH`. Preview/live and
resting/monitored modes cannot share a journal. Keep the live journal across
restarts; deleting it loses reservations, activation time and recovery IDs.
Preview fills are not automatically adopted on first live activation.

The earlier monitored implementation remains available only by explicitly
setting `MANUAL_EXIT_MODE=monitored_reduce_only`. It uses different journals,
waits for the target bid and submits reduce-only IOC exits. That is not the
requested standing-order flow and is no longer the default.

## Railway operator handoff

At the October 29, 2026 inspection, `impartial-prosperity / production / v2`
tracked `fix/manual-exit-latency-20261029` at `69de776` and started
`python manual_exit_runner.py --live`. Its configuration log reported BTC/ETH,
a 5c increment, resting-limit mode, one-second polling and automatic entries off. The WTI update is
prepared separately for operator deployment. `Dockerfile` starts `python manual_exit_runner.py`
in read-only mode. `railway.toml` intentionally omits `startCommand`: Railway
file settings take precedence over dashboard settings, so the file must not
shadow an operator's explicit preview/live selection.

Apply the reviewed WTI revision and expanded `MANUAL_EXIT_SERIES` together,
retaining the existing `/data` volume, state paths and manual-only start command.
Do not deploy the variable change with the old revision: it rejects any series
scope change. WTI is enabled only once the new deployment reports successful
startup with all three series. `MANUAL_SERIES_ADDED` records its activation cutoff.

An operator can choose `python manual_exit_runner.py --live` with
`MANUAL_EXITS_ENABLED=true`, `MANUAL_EXITS_DRY_RUN=false`, and
`MANUAL_EXTERNAL_ORDERS_ARE_MANUAL=true`. Keep `TRADING_ENABLED=false` and
`MANUAL_EXIT_MODE=resting_limit`. The configuration log reports the execution
mode, increment, series, dry-run state and `automatic_entries=false`.

`MANUAL_RESTING_SUBMITTED` confirms an acknowledgement, not a fill.
`MANUAL_RESTING_FILL` reports cumulative filled quantity after reconciliation.
`MANUAL_CANCEL_REQUESTED` does not prove cancellation; reservation release waits
for the terminal exchange status. Paused or closed markets are never assumed sold.

Pause diagnostics include the reconciliation `stage`, the local source file and
line of the failed check, an allowlisted `reason` when available, and the numeric
HTTP status for API failures. Raw exception messages, server response bodies and
credentials are not printed. Events carry their own UTC time; fill-adoption
events also include exchange fill time and detection time so log ingestion
batching is not mistaken for the bot's response time.

Order-direction validation uses Kalshi's canonical `book_side`/`outcome_side`
fields. Deprecated `action`/`side` fields are used only when neither canonical
field is present. Invalid or contradictory canonical fields still block. The
offline tests reproduce a rejected own exit when deprecated direction fields
disagree; this compatibility fix alone does not establish the cause of a live
pause. Diagnose the specific logged check before relaxing inventory safeguards.

## Verification

```bash
python tools/run_offline_tests.py -q test_manual_wti_exits.py test_manual_series_journal.py test_manual_journal_validation.py test_manual_exit_latency.py test_resting_manual_exits.py test_manual_five_cent_exits.py test_manual_limit_exits.py test_hybrid_runtime.py test_co_trading.py
```

Tests block real TCP access and use exchange simulators. They cover posting
below the target, YES/NO and fractional sizing, independent partial-fill targets,
standing reservations, fills without a running worker, restarts, lost responses,
manual reductions, owned cancellations, user-cancel suppression, stale data,
market close, journal isolation, ownership exclusion and prohibited client writes.

API references:
- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/api-reference/orders/cancel-order-v2
- https://docs.kalshi.com/getting_started/order_direction
- https://api.elections.kalshi.com/trade-api/v2/series/KXWTI15M
- https://docs.kalshi.com/getting_started/fixed_point_migration
