# Market-making recovery for v0.9.1 (MM)

The current service submitted entries but Kalshi rejected its resting reduce-only
exits. A later external close left its local inventory stale. Its `/data` directory
was also on the container filesystem, so redeploying before preserving it would
lose the ledger.

## Code changes

- Keep five one-contract entry prices on each side and the $10 initial allocation.
- Use reduce-only IOC limit exits at the executable bid/ask. These exits may pay
  taker fees and may realize losses. Unfilled quantity is canceled and retried
  from fresh snapshots; no incompatible GTC/post-only reduce-only requests.
- Reconcile verified external closing fills and fees, preserve cumulative losses,
  and wait until the next clean market after manual intervention.
- Recognize finalized settlements. Preserve unresolved histories instead of
  guessing or zeroing positions.
- Require a persistent volume and an existing ledger for live MM startup. Write
  state atomically with file and directory fsync, and mirror events to Railway logs.

## Apply with trading stopped

1. Preserve `/data/state.json` and `/data/trades.csv` from the existing container
   before any redeploy. A recovery ZIP contains both files and SHA-256 hashes in
   `manifest.json`. Use a fresh backup if the bot has traded since that snapshot.
   Confirm no open orders or positions remain before stopping the old instance.
2. Stop the old instance without deleting the service or its files. Set
   `TRADING_ENABLED=false` for the replacement deployment. Keep the recovery ZIP
   outside the old container.
3. Attach a Railway volume to **v2**, mounted at `/data`; keep one replica.
   Deploy this release while trading is disabled. A new volume initially hides
   the old container's `/data`, which is why the prior backup is required.
4. Upload the recovery ZIP through the new service's Console file browser to
   `/tmp`, then run the restore utility with its actual uploaded filename:

   ```sh
   python restore_mm_state.py /tmp/mm-recovery-TIMESTAMP.zip
   ```

   The utility checks the manifest and mount, refuses to overwrite existing
   ledger files, and refuses to run unless trading is disabled. It does not
   contact Kalshi or place/cancel orders.
5. Verify the production connection and persistent state without trading:

   ```sh
   python bot.py --check
   python -c 'from storage import require_mm_storage; require_mm_storage("/data/state.json", "/data/trades.csv"); print("Persistent ledger verified")'
   ```

6. Review the deployment and restore results before personally re-enabling
   `TRADING_ENABLED=true`. Retain `EXECUTION_STRATEGY=market_making`. Watch for
   `MM_EXTERNAL_RECONCILED`, `MM_QUOTE`, `MM_EXIT_IOC`, and `MM_FILL` in Deploy Logs.
   A reconciliation error needs investigation; never delete state to bypass it.

The recovery ZIP is private trading data and must not be committed to GitHub.
An old snapshot cannot account for later trades; preserve the latest ledger before
replacing a running container. The code does not set up Railway volumes itself.
