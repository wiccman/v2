# Strike Ruler Kalshi Bot v0.9.4

Production-only Kalshi KXBTC15M bot. It contains no demo or paper mode.

## v0.9.4 fixed prices and independent take-profit monitor

All Strike Ruler entry routes use `ENTRY_PRICE_CENTS=25`. All their held inventory
uses the absolute `EXIT_PRICE_CENTS=45` target, including historical-strike entries.
These are outcome prices: a NO exit at 45 cents sends a YES bid at 55 cents.
Old percentage and per-route profit settings no longer select exit prices.
An old fill above 45 cents can realize a loss at the newly requested fixed target.

After a fill appears in positions, an independent worker persists its 45-cent
target and immediately submits a price-protected, reduce-only IOC. It reconciles
that order and retries remaining holdings, normally after `EXIT_POLL_SECONDS=1`
plus request time. Signal lookups, CF Benchmarks errors and entry polling do not
block this worker. It covers regular, spot, dual and historical entries together
once per net position, and keeps running after the entry/cancellation cutoffs.

**This is a bot-managed target, not a resting exchange-hosted sell order.**
The production API has rejected resting reduce-only orders with
`reduce_only can only be used with IoC orders`. An IOC below the target cancels
unfilled. The monitor retries it; it does not remove reduce-only protection or
post an ordinary sell that could open an opposite position after another fill.
The process must stay online, and polling/API delays can miss short price moves.
Neither an armed target nor an accepted order guarantees an exit or a profit.

Exit receipts live beside `state.json` in `state_take_profit.json` on the same
persistent volume. A durable client ID precedes every submission. Lost responses
are reconciled through order history before another exit is sent. Unknown orders
are never blindly resubmitted. API errors respect Retry-After, pause new buys,
and log `TP_ERROR`; preserve both state files when investigating one.
The worker confirms cancellation of tracked legacy resting exits before taking
over their market. Incompatible old-price entries are canceled without refunding
their market-budget reservations. Entry selection/signal rules are unchanged.

Railway logs now show `TP_MONITOR_STARTED`, `TP_ARMED`, `TP_SUBMITTED`, `TP_FILL`,
`TP_POSITION_FLAT` and errors. `TP_FILL` requires exchange fill counts;
`TP_SUBMITTED` only confirms acceptance. Run one replica with the existing volume.
On deployment verify the startup line reads fixed entry=25c and fixed exit=45c.
If these new variables are absent, those values are the defaults. `--check` and
`TRADING_ENABLED=false` still cannot submit exits or entries.

## v0.9.3 cancellation and budget repair

Strike Ruler stops submitting new buys at **5:00 elapsed** and expires all entry
orders at **6:00 elapsed**, measured from the opening of each 15-minute market.
The exchange expiration is independent of polling. A backup sweep cancels tracked
unfilled orders and retries failures, even when signal or market discovery fails.
V2 cancellation includes `market_ticker` and `exchange_index=-1` so requests reach
the correct exchange shard. Order listings follow every page. Cancellation errors
no longer abandon other entry cancellations or block position exits.

`MARKET_BUDGET_DOLLARS=5` is a **shared per-market allowance** (hard maximum $5).
Every spot, dual, historical and regular entry reserves its limit-price principal
plus a conservative 3-cent-per-contract entry fee cushion before submission.
`ENTRY_BUDGET_DOLLARS` is only the desired size of each order; the remaining market
allowance can reduce or block it. Reservations persist across restarts and remain
consumed after cancellation, rejection or sale, so actual filled spending may be
less than $5. Exit fees are separate. MM batches also obey the same $5 per-market
ceiling in addition to their existing capital checks.

Existing markets without a complete reservation ledger cannot safely receive a
fresh $5 allowance: their tracked unfilled entries are canceled on upgrade and
new entries resume in the next clean market. Existing filled positions continue
through normal exit management. Persisted client IDs recover orders whose POST
acknowledgement was lost. Do not erase state to reset a market's budget.

Regular and historical exits use the shared fixed 45-cent monitor described above.
Exit monitoring continues after 6:00. `TAKE_PROFIT_CENTS` and
`TAKE_PROFIT_PERCENT` are unused. The old per-order dual/historical TTL settings
are superseded by the fixed six-minute market deadline in Strike Ruler mode.

Cancellation API reference: [V2 cancel routing](https://docs.kalshi.com/api-reference/orders/cancel-order-v2).

## Market-making mode (MM)

This release adds an **opt-in alternative execution mode**. The default remains
`EXECUTION_STRATEGY=strike_ruler`, so deploying the code alone does not activate MM.
The historical prediction collection rules are unchanged. Tests use a simulated
exchange. This repair adds minute-based entries, IOC exits, and durable storage checks.

For MM, configure `EXECUTION_STRATEGY=market_making`. The existing
`TRADING_ENABLED` master switch still applies. Run only one service/replica on
this account and market; do not run the old bot or trade this ticker manually
at the same time. Keep the existing persistent `/data` volume. A process lock
prevents concurrent bots sharing that volume; it cannot lock a different service
with a separate volume. Switching modes waits for a clean contract rather than
taking over the other strategy's inventory.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MM_BUDGET_DOLLARS` | `10` | Initial MM risk capital across markets; each market also has the $5 cap |
| `MARKET_BUDGET_DOLLARS` | `5` | Shared entry allowance per market, hard maximum $5 |
| `MM_QUOTE_CONTRACTS` | `1` | Contracts at each price level |
| `MM_LEVELS_PER_SIDE` | `5` | Five one-contract quotes on each side; maximum unmatched inventory is five |
| `MM_LEVEL_STEP_CENTS` | `1` | Minimum price spacing between successive levels |
| `MM_SPREAD_CENTS` | `8` | Minimum full bid-to-ask quote width |
| `MM_FEE_RESERVE_CENTS` | `2` | Assumed fee allowance per contract per fill |
| `MM_QUOTE_TTL_SECONDS` | `15` | Exchange-side quote expiration |
| `MM_POLL_SECONDS` | `3` | Delay between completed polling cycles |
| `MM_MAX_DATA_AGE_SECONDS` | `5` | Maximum book-request/processing age before submission |
| Entry timing (fixed) | minutes `0–4` | At most one batch per minute; no entries at or after 5:00 elapsed |
| `MM_MAX_MID_MOVE_CENTS` | `10` | Midpoint jump triggering a pause |
| `MM_COOLDOWN_SECONDS` | `30` | Pause following that jump |

- Quotes follow the midpoint of external YES/NO bids after subtracting our own
  resting size. This midpoint is a pricing heuristic, not a calibrated fair-value
  model. Prices snap outward to market ticks. Empty/crossed books stop quoting.
- The two five-level ladders are post-only, expiring limit orders with self-trade prevention.
  An illustrative 46-cent YES bid and 54-cent YES ask correspond to buying YES
  and NO at 46 cents. Equal fills earn 8 cents gross, before fees and other losses.
- With a 50-cent midpoint the default YES bids are 46, 45, 44, 43, 42 cents;
  NO bids are also 46, 45, 44, 43, 42 cents (YES asks 54 through 58 cents).
  That reserves $4.40 principal plus a $0.30 fee cushion against the market cap for all ten quotes.
  The prices move with the book; they are not permanently fixed at these levels.
  A complete ladder must fit available cash and remaining budget or none is posted.
  Quotes submit in one batch; partial rejections trigger cancellation of accepted
  orders. The account's API tier must support ten-order batches.
- Once even a partial position is detected, new exposure pauses and only a
  reduce-only exit ladder works exactly the held quantity, including fractions.
  The first outside ask (YES holdings) or bid (NO holdings) becomes a saved exit target.
  At a price boundary, remaining exit quantity consolidates at the last valid tick.
  Kalshi requires reduce-only exits to be immediate-or-cancel. The bot monitors
  these saved prices and submits only executable exit levels, with post-only off.
  Unfilled amounts remain held and are retried while the target is reachable.
  IOC fills may incur taker fees;
  **it may realize a loss** and does not use the legacy 15% or 10-cent target.
- Entry batches are reserved durably once per elapsed minute, from minute 0
  through minute 4. A restart cannot repeat the same minute's batch. Unfilled
  orders expire after their TTL; the bot does not renew them within that minute.
  Missed minutes are not replayed. Existing inventory, budget, stale-data, and
  other safety checks may prevent a batch; five fills are not guaranteed.
  A confirmed cancellation and another polling cycle precede replacement. Polling plus
  network time is slower than a streaming market maker; fast adverse moves can
  still fill stale quotes.
- Persisted client order IDs recover orders after an ambiguous POST or restart.
  Unresolved submissions, foreign orders, or mismatched positions block quoting.
  If a submission remains unresolved, inspect its saved client ID and exchange
  history before repairing state; do not clear it just to force a retry.
  Cumulative fills and fees come from order status, including cancel-race fills.
- The conservative cash ledger costs fills at their limit and deducts actual fees;
  realized losses consume the $10 allocation across markets/restarts. New quotes
  must fit both remaining allocation and available account cash, with a fee reserve.
  Profits do not increase quote size. Budget changes apply against the existing
  ledger, not a fresh $10 allocation. Do not delete state to restart a spent budget.
- Validate the fee allowance against current fees for the selected market.
  The 8-cent spread and 2-cent fee reserve are test defaults, not evidence of an edge.
- New-entry orders expire by five minutes after opening; reduce-only exits continue
  until close. Unfilled inventory may settle for a loss. There is no forced market
  liquidation or guaranteed take profit. Unsettled prior MM inventory blocks new
  markets until settlement is confirmed.
- On errors/shutdown the bot attempts to cancel its own quotes. Exchange-side TTL
  remains the fallback during connectivity loss. Canceling orders does not close
  inventory. State persistence and a single running instance are required.

Run `python -m pytest -q` for offline verification; it does not authenticate or
send trades. `python bot.py --check` remains a read-only production access check.
Logs identify `MM_ENTRY_MINUTE`, `MM_QUOTE`, `MM_EXIT_TARGET`, `MM_EXIT_IOC`,
`MM_FILL`, and blocked/pause conditions separately.

API references: [V2 orders](https://docs.kalshi.com/api-reference/orders/create-order-v2),
[order status](https://docs.kalshi.com/api-reference/orders/get-order),
[orderbooks](https://docs.kalshi.com/getting_started/orderbook_responses), and
[fees](https://kalshi.com/fee-schedule).

## Locked execution rules (default Strike Ruler mode only)

- Strike Ruler relative-position majority: 2–3 prior settlements below the current strike predicts YES; 2–3 above predicts NO.
- The historical Strike Ruler direction is fixed for the entire 15-minute contract.
- The bot records three prediction snapshots at minutes 2, 4, and 6.
- Each snapshot refreshes confidence from the current Kalshi ask price for the predicted side.
- Live confidence cannot reverse YES to NO or NO to YES.
- Gap average is observational only and never flips a prediction.
- During minutes 0–2, place one 25-cent YES limit buy if Kalshi's authenticated CF Benchmarks BRTI value is at least $80 above the Kalshi strike.
- This one-time spot-trigger entry uses the configured per-entry budget and shared market cap.
- If the Kalshi account lacks CF Benchmarks passthrough access, no spot-trigger order is placed and the API error is logged.
- Up to 7 separate limit purchases per contract.
- Each purchase budgets $0.77 of contract value.
- At minute 2, independently post one YES limit buy and one NO limit buy at 25 cents, each using the same $0.77 purchase budget.
- Both 25-cent orders expire at minute 6 of the contract; the bot also cancels any tracked unfilled remainder on its next polling cycle.
- Filling one side does not cancel the other side. Net holdings use the shared 45-cent exit target.
- The last three completed KXBTC15M strikes are also watched as BTC support/resistance levels during minutes 2–5.
- When live BRTI comes within $25 of a prior strike, an approach from below posts a 25-cent NO rejection order; an approach from above posts a 25-cent YES bounce order.
- Each historical-strike level triggers at most once per current contract, uses the same $0.77 order budget, and expires at minute 6 of the contract if unfilled.
- Historical-strike fills use the same absolute 45-cent target as every other entry route.
- HIGH and MODERATE signals place 25-cent limit buys on the predicted side.
- Entry checks run every 7 seconds from minute 2 until minute 5.
- At minute 5, all new entries stop. At minute 6, unfilled entries expire and the backup cancellation sweep retries any remaining tracked orders. The third prediction snapshot at minute 6 remains observational.
- The three predicted-side ask prices are averaged as the contract's final confidence.
- The former minute-12 entry is disabled by the five-minute cutoff.
- After a buy fills, the independent monitor arms and submits a reduce-only IOC at the fixed 45-cent outcome target. The exchange tests whether it can execute at that limit or better.
- Partial and unfilled exits are reconciled before retrying the remaining net holdings. There is one exit owner for all Strike Ruler routes.
- The target ignores fees; there is no automatic stop-loss sell.
- Rejected take-profit orders log Kalshi's response and pause new entries. Temporary errors retry after at least five seconds or a longer Retry-After. Ambiguous responses require reconciliation first.
- Exit monitoring continues after minute 5 until market close. The bot must stay online; polling/network latency may miss a brief target touch. Unfilled holdings can settle for a loss.
- No daily-loss limit.

All these entry routes compete for the shared $5 market allowance. The per-entry size and purchase count settings cannot increase that cap.

## Railway setup

1. In Railway, create a project from this GitHub repository.
2. Open **Variables** and add every variable shown in `.env.example`.
3. Enter your Kalshi key ID as `KALSHI_API_KEY_ID`.
4. Convert your private-key text file to base64 and enter that single line as `KALSHI_PRIVATE_KEY_B64`.
5. Keep `TRADING_ENABLED=false` during the first deployment.
6. Run `python bot.py --check` in Railway. This only checks authentication and cannot place orders.
7. If the check succeeds, change `TRADING_ENABLED=true` and redeploy.
8. Before enabling trading, attach a persistent volume to the service at `/data`.
   Both `STATE_PATH` and `LOG_PATH` must be on that volume. Migrate the existing
   `state.json` and `trades.csv` before restarting an existing bot; never reset the
   ledger to bypass a budget or reconciliation block. MM waits for state restoration
   on an empty volume. For a genuinely new bot only, initialize `state.json` with
   `{"markets": {}}` after confirming there is no previous ledger to restore.

The `/data` volume preserves per-market purchase counters, one-time entry flags, and trade logs across restarts and deployments. Do not enable live trading without this volume; otherwise a restart during an active market can allow duplicate entries.

Never upload or share your Kalshi private key.
