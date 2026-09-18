# Strike Ruler Kalshi Bot v0.5.1

Production-only Kalshi KXBTC15M bot. It contains no demo or paper mode.

## Locked execution rules

- Strike Ruler relative-position majority: 2–3 prior settlements below the current strike predicts YES; 2–3 above predicts NO.
- The historical Strike Ruler direction is fixed for the entire 15-minute contract.
- The bot records three prediction snapshots at minutes 2, 4, and 6.
- Each snapshot refreshes confidence from the current Kalshi ask price for the predicted side.
- Live confidence cannot reverse YES to NO or NO to YES.
- Gap average is observational only and never flips a prediction.
- Up to 7 separate limit purchases per contract.
- Each purchase budgets $0.77 of contract value.
- Buy only HIGH-confidence signals (all 3 prior settlements on the same side of the strike).
- MODERATE 2-of-3 signals are recorded but never traded.
- Buy only when the predicted side costs 10–47 cents.
- Entry checks run every 7 seconds from minute 2 until minute 6.
- At minute 6, the third prediction is recorded, new entries stop, and unfilled entries are canceled.
- Sell the entire held position when its executable bid reaches a 15% gross gain over the weighted-average fill price.
- The 15% target ignores fees; the 4-cent emergency exit remains active.
- Exit monitoring continues after minute 6; otherwise positions are held through settlement.
- No daily-loss limit.

Maximum planned entry principal is $5.39 per market before fees.

## Railway setup

1. In Railway, create a project from this GitHub repository.
2. Open **Variables** and add every variable shown in `.env.example`.
3. Enter your Kalshi key ID as `KALSHI_API_KEY_ID`.
4. Convert your private-key text file to base64 and enter that single line as `KALSHI_PRIVATE_KEY_B64`.
5. Keep `TRADING_ENABLED=false` during the first deployment.
6. Run `python bot.py --check` in Railway. This only checks authentication and cannot place orders.
7. If the check succeeds, change `TRADING_ENABLED=true` and redeploy.

Never upload or share your Kalshi private key.
