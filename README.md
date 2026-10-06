# OfflinePOS: A Priority-Scheduled, Conflict-Aware Retail Sync System

**Team Kernel Panic** — OS + DBMS course project.

A point-of-sale app that keeps working with **no internet**. Sales happen
locally (scan, pay, print receipt) in SQLite; when connectivity returns,
transactions sync automatically to a central database. Built for remote
stores and pop-up shops where Shopify/Square/Toast assume you're always
online.

## OS + DBMS concepts demonstrated
- **Priority scheduling** — billing runs on a dedicated worker; it never
  waits on background sync/retry jobs.
- **Mutexes / locking** — `BEGIN IMMEDIATE` serialises writers; per-module
  locks guard shared state.
- **ACID transactions** — each sale is all-or-nothing in WAL-mode SQLite.
- **Delta sync** — only changed facts (transactions, stock deltas, product
  updates) move, never full dumps.
- **Conflict resolution** — stock merges commutatively (no conflicts);
  product master data uses last-write-wins on `(updated_at, updated_by)`
  with losers logged for review (ADR-001).
- **Idempotency + exponential backoff** — retried pushes converge to
  exactly-once (ADR-004).

## Hybrid chatbot
Offline: intent matching over local SQLite (sales totals, stock, sync
status). Online: LLM API (stubbed, see ADR-002) with graceful fallback.
Shares the sync engine's connectivity state machine.

## Quickstart
```bash
python3 simulations/run_all.py
```
Runs 7 simulation scenarios (offline sale→sync, price conflict,
delta merge, flaky-link idempotency, billing priority, offline chatbot,
clock skew). Results land in `SIMULATION_RESULTS.md`.

## Layout
```
offlinepos/        # product code
  local_db.py      # terminal SQLite store (WAL, ACID)
  central_db.py    # central DB (simulated; MySQL-shaped interface)
  net.py           # shared connectivity state machine
  scheduler.py     # priority scheduler, dedicated billing lane
  sync_engine.py   # delta sync, LWW, backoff, pull convergence
  pos.py           # Terminal: scan/checkout/receipt
  chatbot.py       # hybrid assistant
  dashboard.py     # health dashboard
  chaos.py         # failure injection
simulations/run_all.py
docs/              # ADRs, limitations, chaos notes
```

## Docs
- `docs/ARCHITECTURE_DECISIONS.md` — ADRs 001–005
- `docs/KNOWN_LIMITATIONS.md`
- `docs/CHAOS_TESTING.md`
- `CONTRIBUTING.md` — commit convention
- `SIMULATION_RESULTS.md` — latest run
