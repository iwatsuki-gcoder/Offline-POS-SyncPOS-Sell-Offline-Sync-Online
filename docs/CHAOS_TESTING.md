# Chaos Testing — OfflinePOS

Failure injection lives in `offlinepos/chaos.py` (`Chaos` class) and is
exercised by the simulation suite. Failure modes:

## 1. Dropped connection mid-push (`fail_push_times = N`)
The transport raises `ConnectionError` N times before succeeding. The sync
engine must retry with exponential backoff and still converge to
exactly-once. **Covered by S4** (3 drops → 3 retries → 1 central txn).

## 2. Duplicate batch delivery (`duplicate_push = True`)
The same delta batch is applied twice, simulating a retry whose
acknowledgement was lost. The central must dedupe via `idempotency_key`
(transactions) and `applied_deltas` (stock). **Covered by S4.**

## 3. Clock skew (offset `LocalDB.clock`)
A terminal's clock runs fast/slow. LWW ordering follows the skewed
timestamp, so the "wrong" write can win; the conflict must still be
logged for manual review. **Covered by S7** (+5000s skew).

## 4. Saturated background lane (scheduling chaos)
The background worker is blocked with long sync/maintenance jobs while a
checkout arrives. Billing must still complete in milliseconds via its
dedicated lane. **Covered by S5.**

## Suggested additions (not yet implemented)
- **Power-loss mid-transaction:** kill -9 the process between `BEGIN
  IMMEDIATE` and `COMMIT`; on restart the sale must be fully present or
  fully absent (WAL + atomic commit already designed for this).
- **Partial batch:** drop the connection *after* central applied the batch
  but before the terminal marked rows synced; the retry must dedupe
  everything.
- **Interleaved price wars:** N terminals hammering the same product price
  offline, then syncing in random order; assert central converges to the
  max timestamp and exactly N-1 conflicts are logged.
