# Architecture Decision Records — OfflinePOS

## ADR-001: Conflict resolution strategy

**Status:** accepted

**Context.** Multiple offline terminals can change the same data and sync
later. The DBMS grading rubric asks explicitly how conflicts are resolved,
so "conflict resolution" could not stay a buzzword.

**Decision.** Two-track strategy:

1. **Inventory (stock): delta merge, no conflicts possible.** Every sale or
   restock appends a commutative fact (`stock = stock + delta`). Addition
   commutes, so two terminals selling offline never conflict however their
   syncs interleave. Deltas are applied exactly once via an
   `applied_deltas` table on the central side.
2. **Product master data (name/price): last-write-wins (LWW)** ordered by
   `(updated_at, updated_by)`. The timestamp decides; the terminal id breaks
   ties deterministically. The losing write is **not silently dropped**: it
   is returned in the sync report and recorded in the terminal's local
   `conflicts` table plus audit log for manual review.

**Consequences.** Stock is always correct without human intervention.
Master-data conflicts resolve automatically but remain visible, which is
the honest trade-off for an LWW scheme. See ADR-005 and S2/S7 simulations.

## ADR-002: Hybrid chatbot

**Status:** accepted (online path live since 2026-10-06)

**Context.** The assistant must be useful with zero connectivity (the whole
point of the product) but richer when online.

**Decision.**
- **Offline:** deterministic intent matching (regex over the local SQLite
  data): sales totals, stock checks, sync status, help. No model, no
  network, ~instant.
- **Online:** a real LLM call via `offlinepos/llm.py` — a stdlib-only
  OpenAI-compatible `/chat/completions` client (`urllib`, no SDK). Works
  with OpenAI, any OpenAI-compatible provider, or a local Ollama
  (`OFFLINEPOS_LLM_BASE_URL=http://localhost:11434/v1`). Config:
  `OFFLINEPOS_LLM_BASE_URL` / `OFFLINEPOS_LLM_API_KEY` /
  `OFFLINEPOS_LLM_MODEL` (default `gpt-4o-mini`) / `OFFLINEPOS_LLM_TIMEOUT`.
- **Grounding:** the system prompt injects live store context (today's
  sales, product count, low-stock list, pending sync counts, last sync),
  so the LLM answers from facts instead of inventing numbers.
- **Fallback:** if no key is configured, the network fails, or the
  response is malformed (`LLMError`), the bot degrades to offline intents
  and says so, instead of erroring. Billing never waits on the network:
  chat runs at `CHATBOT` priority with a 20s timeout.
- The bot subscribes to the **same `Connectivity` state machine** as the
  sync engine (no parallel "am I online?" logic to drift apart).

**Consequences.** Examiners asking "what if the API fails mid-session?"
get a concrete answer: graceful degradation to local intents, and the
sync/chatbot state can never disagree. S12 proves the live call and the
fallback against a fake server.

## ADR-003: Priority scheduling

**Status:** accepted

**Context.** OS concept: the billing thread must never wait on background
sync/retry work, or customers queue at the counter during a sync storm.

**Decision.** Two lanes instead of one shared pool:
- a **dedicated billing worker** (FIFO) that only runs checkouts;
- a **background worker** draining a priority heap
  (CHATBOT < SYNC < RETRY < MAINTENANCE).

Wait-time statistics per lane are recorded so the property is measurable
(S5 proves a checkout completes in milliseconds while the background lane
is blocked for seconds).

**Alternatives considered.** A single priority queue with billing at top
priority still serialises billing behind a long-running sync job already
dequeued; the dedicated lane removes that head-of-line blocking entirely.

## ADR-004: Idempotency and retry

**Status:** accepted

**Decision.** Every transaction carries an `idempotency_key`
(`{terminal_id}:{txn_id}`, UNIQUE on central). Every stock delta carries a
`delta_id` recorded in `applied_deltas`. Retries therefore converge to
exactly-once: a duplicated batch is a no-op. Backoff is exponential with
jitter (`base * 2^attempt + jitter`, capped) to avoid thundering-herd
reconnects when connectivity flaps for a whole store.

## ADR-005: Central database is simulated

**Status:** accepted (for this build)

**Context.** The target is MySQL / MS SQL Server, but the build/test
environment here has no server.

**Decision.** `central_db.CentralDB` implements the exact narrow interface
the sync engine needs (`apply_batch`, `fetch_products`, …) on top of a
SQLite file. Swapping in `mysql.connector` touches only this module. All
SQL used is portable (no SQLite-only tricks in the sync contract).

## ADR-006: Tax calculation

**Status:** accepted

**Context.** The POS and receipt printer had no tax: receipts showed a
single pre-tax total. Real billing needs per-product tax rates (e.g. GST
slabs) applied at checkout and printed on the receipt.

**Decision.**
- Each product carries `tax_rate` (percent, e.g. `18` = 18%), a
  catalog-seeded column on `products` (ALTER TABLE migration for existing
  DBs, default 0).
- Tax is **exclusive** (added on top of price) and computed **per line**:
  `line_tax = round(price * qty * rate / 100, 2)`, inside the same
  `BEGIN IMMEDIATE` ACID transaction as the sale — the breakdown can never
  disagree with the recorded sale.
- `transactions` stores `subtotal`, `tax_total`, and `total` (grand total;
  `total` keeps its old meaning for back-compat). Receipts (text, ESC/POS,
  web modal) print SUBTOTAL / TAX / TOTAL; when every line shares one
  rate the receipt shows `TAX (18%)`.
- `tax_rate` is master data seeded from `catalog/products.csv`. It is
  **not** part of the LWW `product_updates` sync yet (see
  KNOWN_LIMITATIONS.md): changing a rate means re-seeding the catalog.

**Consequences.** Billing, receipts, and the web UI all show tax with no
new network dependency — the offline story is unchanged. S11 proves the
math, persistence, printing, sync carry-over, and the migration of
pre-tax databases.
