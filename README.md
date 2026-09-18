# Strike Ruler Kalshi Bot v0.7.8

Production-only Kalshi KXBTC15M bot. It contains no demo or paper mode.

## Locked execution rules

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
- Buy HIGH-confidence signals (all 3 prior settlements on the same side of the strike) when the predicted side costs 10–47 cents.
- Buy MODERATE 2-of-3 signals only when the predicted side costs 10–30 cents.
- Entry checks run every 7 seconds from minute 2 until minute 6.
- At minute 6, the third prediction is recorded, new entries stop, and unfilled entries are canceled.
- The three predicted-side ask prices are averaged as the contract's final confidence.
- From minute 12 until minute 15, place exactly one additional $0.77 entry when final average confidence is at least 65%.
- The final entry uses the original fixed Strike Ruler direction. This final phase can trade either HIGH or MODERATE base signals when the 65% average threshold is met.
- After a buy fills, keep a reduce-only resting sell for the full held position at a 15% gross gain over the weighted-average fill price.
- The target rounds up to the next valid Kalshi price tick and never exceeds the market's highest tradable price.
- Weighted-average entry price supports Kalshi's current `outcome_side` fill schema and legacy fills.
- Added fills cancel and replace the resting sell so its quantity and weighted-average target stay current.
- The gross target ignores fees; there is no automatic stop-loss sell.
- Rejected take-profit orders log Kalshi's response details and wait 60 seconds before retrying the same order.
- Exit monitoring continues after minute 6; otherwise positions are held through settlement.
- No daily-loss limit.

Maximum planned entry principal is $6.93 per market before fees: one spot-trigger entry, seven regular entries, and one final entry at $0.77 each.

## Railway setup

1. In Railway, create a project from this GitHub repository.
2. Open **Variables** and add every variable shown in `.env.example`.
3. Enter your Kalshi key ID as `KALSHI_API_KEY_ID`.
4. Convert your private-key text file to base64 and enter that single line as `KALSHI_PRIVATE_KEY_B64`.
5. Keep `TRADING_ENABLED=false` during the first deployment.
6. Run `python bot.py --check` in Railway. This only checks authentication and cannot place orders.
7. If the check succeeds, change `TRADING_ENABLED=true` and redeploy.
8. In Railway, attach a persistent volume to the service and set its mount path to `/data`.

The `/data` volume preserves per-market purchase counters, one-time entry flags, and trade logs across restarts and deployments. Do not enable live trading without this volume; otherwise a restart during an active market can allow duplicate entries.

Never upload or share your Kalshi private key.
