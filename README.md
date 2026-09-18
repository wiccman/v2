# Strike Ruler Kalshi Bot v0.2

Production-only Kalshi KXBTC15M bot. It contains no demo or paper mode.

## Locked execution rules

- HIGH-confidence Strike Ruler signal only.
- Up to 7 separate limit purchases per contract.
- Each purchase budgets $0.77 of contract value.
- Buy only when the predicted side costs 10–47 cents.
- Entries run from minute 2 until minute 6; unfilled entries expire and are canceled at minute 6.
- Sell the maximum held position at 4 cents or lower or 96 cents or higher.
- Otherwise hold through settlement.
- No daily-loss limit.

Maximum planned entry principal is $5.39 per market before fees.

## Upload to GitHub

1. Unzip this package.
2. Open your empty private GitHub repository.
3. Select **Add file**, then **Upload files**.
4. Drag all files and folders from inside the unzipped folder into GitHub.
5. Select **Commit changes**.

Never upload your Kalshi API text/private-key file to GitHub.

## Railway setup

1. In Railway, create a project from the private GitHub repository.
2. Open **Variables** and add every variable shown in `.env.example`.
3. Enter your Kalshi key ID as `KALSHI_API_KEY_ID`.
4. Convert your private-key text file to base64 and enter that single line as `KALSHI_PRIVATE_KEY_B64`.
5. Keep `TRADING_ENABLED=false` during the first deployment.
6. Run `python bot.py --check` in Railway. This only checks authentication and cannot place orders.
7. If the check succeeds, change `TRADING_ENABLED=true` and redeploy.

Windows PowerShell command for step 4:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\kalshi-private-key.txt"))
```

Do not send or paste the private key into ChatGPT.

Official API documentation:

- https://docs.kalshi.com/getting_started/api_keys
- https://docs.kalshi.com/api-reference/orders/create-order-v2
- https://docs.kalshi.com/api-reference/orders/cancel-order-v2
- https://docs.kalshi.com/api-reference/portfolio/get-positions
