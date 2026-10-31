# Buy-stop paper panel

A standalone paper-only control panel for live Kalshi public market data.
It has no credentials, account access, order-submission endpoint, or live mode.
It does not import, start, or change the existing trading bot.

Run `python app.py`, then visit http://localhost:8080. Run the offline tests with
`python -m unittest -v test_panel.py`. Python 3.12+, no external dependencies.

Choose a 15-minute market, YES or NO, trigger cents, cap cents, and contracts.
Arm the paper trigger. It first observes an asking price below the trigger,
then fires on a fresh crossing. Prices beyond the cap consume the trigger as
skipped. The next separately fetched order book estimates an IOC fill at or
below the cap, including partial fills. No actual order is sent.

Quotes are polled about once per second. Short spikes can be missed. Estimated
fills use displayed depth and exclude fees and queue competition. This is not
an execution-speed benchmark, profitability test, or guarantee of live fills.
At most 3 active triggers per browser session and 30 across the service.

Each browser gets a separate HttpOnly session. Closing the browser does not
stop server-side monitoring. Restarts cancel active paper triggers; the user
must arm them again. Paper history is saved in `data/paper.json`, or the path in
`PAPER_STATE_PATH`. Without a persistent volume history can disappear on deploy.
There is no take-profit/stop-loss simulation in this panel and no connection
to the running bot's exit journals.

For a separate Railway service use the repository's `/buy_stop_panel` root,
its Dockerfile, start command `python app.py`, and healthcheck `/health`.
Do not change the current trading service's root, command, or variables.
No Kalshi API keys are needed. Use a single service replica.
