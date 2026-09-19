# Strike Ruler Kalshi Bot v0.9.1 (MM)

Production-only Kalshi KXBTC15M bot. It contains no demo or paper mode.

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
| `MM_BUDGET_DOLLARS` | `10` | Initial MM risk capital; $10 maximum in this beta |
| `MM_QUOTE_CONTRACTS` | `1` | Contracts at each price level |
| `MM_LEVELS_PER_SIDE` | `5` | Five one-contract quotes on each side; maximum unmatched inventory is five |
| `MM_LEVEL_STEP_CENTS` | `1` | Minimum price spacing between successive levels |
| `MM_SPREAD_CENTS` | `8` | Minimum full bid-to-ask quote width |
| `MM_FEE_RESERVE_CENTS` | `2` | Assumed fee allowance per contract per fill |
| `MM_QUOTE_TTL_SECONDS` | `15` | Exchange-side quote expiration |
| `MM_POLL_SECONDS` | `3` | Delay between completed polling cycles |
| `MM_MAX_DATA_AGE_SECONDS` | `5` | Maximum book-request/processing age before submission |
| Entry timing (fixed) | minutes `0–6` | At most one batch per minute; no entries at or after 7:00 elapsed |
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
  That reserves $4.40 principal plus a $0.20 fee allowance for all ten quotes.
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
  through minute 6. A restart cannot repeat the same minute's batch. Unfilled
  orders expire after their TTL; the bot does not renew them within that minute.
  Missed minutes are not replayed. Existing inventory, budget, stale-data, and
  other safety checks may prevent a batch; seven fills are not guaranteed.
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
- New-entry orders expire by seven minutes after opening; reduce-only exits continue
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
- During minutes 0–2, buy YES once at the current ask if Kalshi's authenticated CF Benchmarks BRTI value is at least $80 above the Kalshi strike.
- This one-time spot-trigger entry budgets $0.77 and does not use the normal 47-cent entry cap.
- If the Kalshi account lacks CF Benchmarks passthrough access, no spot-trigger order is placed and the API error is logged.
- Up to 7 separate limit purchases per contract.
- Each purchase budgets $0.77 of contract value.
- At minute 2, independently post one YES limit buy and one NO limit buy at 25 cents, each using the same $0.77 purchase budget.
- Both 25-cent orders expire exactly five minutes after placement; the bot also cancels any tracked unfilled remainder on its next polling cycle.
- Filling one side does not cancel the other side. Any resulting open position uses the same 15% take-profit management below.
- The last three completed KXBTC15M strikes are also watched as BTC support/resistance levels during minutes 2–6.
- When live BRTI comes within $25 of a prior strike, an approach from below posts a 25-cent NO rejection order; an approach from above posts a 25-cent YES bounce order.
- Each historical-strike level triggers at most once per current contract, uses the same $0.77 order budget, and expires after five minutes if unfilled.
- Historical-strike fills reserve their own reduce-only exit 10 cents above their actual weighted-average fill; a 25-cent fill exits at 35 cents while other inventory retains the 15% target.
- Buy HIGH-confidence signals (all 3 prior settlements on the same side of the strike) when the predicted side costs 10–47 cents.
- Buy MODERATE 2-of-3 signals only when the predicted side costs 10–30 cents.
- Entry checks run every 7 seconds from minute 2 until minute 6.
- At minute 6, the third prediction is recorded, new entries stop, and unfilled entries are canceled.
- The three predicted-side ask prices are averaged as the contract's final confidence.
- From minute 12 until minute 15, place exactly one additional $0.77 entry when final average confidence is at least 65%.
- The final entry uses the original fixed Strike Ruler direction. This final phase can trade either HIGH or MODERATE base signals when the 65% average threshold is met.
- After a buy fills, keep a reduce-only resting sell for the non-historical quantity at a 15% gross gain over its weighted-average fill price. Historical-strike inventory retains its separate 10-cent target.
- v0.8.1 fixes overlapping exit quantities for YES and NO holdings. On the next successful exit-management cycle, tracked regular sell orders from older versions are canceled and replaced once using the corrected quantity.
- The target rounds up to the next valid Kalshi price tick and never exceeds the market's highest tradable price.
- Weighted-average entry price supports Kalshi's current `outcome_side` fill schema and legacy fills.
- Added fills cancel and replace the resting sell so its quantity and weighted-average target stay current.
- The gross target ignores fees; there is no automatic stop-loss sell.
- Rejected take-profit orders log Kalshi's response details and wait 60 seconds before retrying the same order.
- Exit monitoring continues after minute 6; otherwise positions are held through settlement.
- No daily-loss limit.

Maximum planned entry principal is $10.78 per market before fees: three historical-strike entries, two dual-sided 25-cent entries, one spot-trigger entry, seven regular entries, and one final entry at $0.77 each.

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
