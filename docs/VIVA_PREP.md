# Viva / Defense Prep — OfflinePOS

Model answers to the questions examiners are most likely to ask, each one
grounded in a specific architecture decision (ADR) or simulation you can
re-run live. The full suite runs in ~3 s:

    python3 simulations/run_all.py    # 13/13 PASS -> SIMULATION_RESULTS.md

## DBMS questions

**Q: How do you resolve conflicts when two terminals edit the same data offline?**
A: Two-track strategy (ADR-001). Inventory is conflict-*free* by design:
every sale/restock appends a commutative stock delta, and addition
commutes, so interleaved offline syncs can never disagree — proven in S3,
where two terminals selling offline merged 100 → 95 with zero conflicts.
Product master data (price/name) uses last-write-wins ordered by
`(updated_at, updated_by)`; the timestamp decides, the terminal id breaks
ties deterministically. The loser is never silently dropped — it lands in
the sync report, the terminal's local `conflicts` table and the audit
log. Live demo: re-run `run_all.py` and show S2 (price $11 vs $12, the
newer $12 won, loser logged).

**Q: Why LWW instead of vector clocks or manual merge?**
A: We scoped it honestly. For master data (a handful of price/name
fields), LWW with a visible loser is simpler, cheaper, and adequate — the
trade-off is documented in ADR-001 and Known Limitations #4 (no merge UI
yet). We'd upgrade to vector clocks only if we needed causal ordering
across many field types, which is future work, not today's requirement.

**Q: How do you prove "exactly-once" sync and not at-least-once with dedup hopes?**
A: Every transaction carries an idempotency key
`{terminal_id}:{txn_id}`, UNIQUE on the central side; every stock delta
carries a `delta_id` recorded in `applied_deltas` (ADR-004). A duplicated
batch is a no-op by construction. S4 proves it empirically: 3 mid-push
connection drops → 3 backoff retries → exactly 1 central transaction, and
a deliberate duplicate push was deduped. The claim is structural (unique
constraints), not probabilistic.

**Q: Why delta sync instead of pushing the whole database?**
A: Bandwidth and time on flaky links. Only changed rows (transactions,
stock deltas, master-data updates) move. S1 shows the minimal case: one
offline sale pushed exactly 1 transaction; central stock converged 20→18.

**Q: How do local sales stay reliable if the terminal crashes or loses power?**
A: SQLite in WAL mode with sales wrapped in ACID transactions
(`BEGIN IMMEDIATE`) — a crashed sale either commits fully or not at all,
never half-written. The sync engine then retries idempotently, so a
crash before sync just means "retry later," not data loss.

**Q: Clock skew — your LWW trusts terminal clocks. Is that a hole?**
A: Yes, and we demonstrate the hole rather than hide it (Known Limitations
#3). S7: a terminal with +5000 s skew won LWW and the conflict was logged
for manual review. We detect (the log), we don't yet prevent — NTP
enforcement or a vector-clock upgrade is listed as future work.

## OS questions

**Q: The UI has an offline/online toggle I can flip with no internet — isn't that fake?**
A: It's a deliberate simulation control, not a bug. `net.py` is a shared
connectivity state machine: in manual mode (default) the toggle, the
chaos injector, or tests drive it, so demos of "link drops mid-sale" are
deterministic. Flipping it with no real internet is safe — sync fails,
backs off exponentially (S4), and the chatbot falls back to offline
intents. Pressing **Auto** (or `OFFLINEPOS_NET_MODE=auto`) switches the
same state machine to a real socket probe every 10 s, so the pill, the
sync engine, and the chatbot all follow actual connectivity. S13 proves
the probe and the auto-driven transitions.

**Q: How do you guarantee a customer never waits at the counter because of sync work?**
A: Not one shared priority queue — a dedicated billing lane (ADR-003). One
worker runs checkouts FIFO and does nothing else; a separate background
worker drains a priority heap (CHATBOT < SYNC < RETRY < MAINTENANCE). A
single queue would still serialize billing behind a long-running sync job
already dequeued — the dedicated lane removes head-of-line blocking
entirely. S5 proves it: with the background lane blocked for ~2 s,
checkout finished in ~0.001 s.

**Q: Where do mutexes/locking come in?**
A: Concurrency between local writes (sales landing in SQLite) and remote
writes (sync engine pulling central state) is serialized so neither
clobbers the other mid-transaction; the billing lane also serializes
concurrent checkouts on one terminal.

**Q: Why exponential backoff with jitter, not fixed retries?**
A: When connectivity flaps for a whole store, every terminal reconnecting
at the same instant creates a thundering-herd storm. Backoff
`base * 2^attempt + jitter` (capped) staggers reconnects — covered in
ADR-004 and exercised in S4.

## Chatbot questions

**Q: How can a chatbot be useful with no internet — isn't that the whole point of the product?**
A: Offline it runs deterministic intent matching (regex over local SQLite):
sales totals, stock checks, sync status — no model, no network, instant.
Online it delegates open-ended questions to an LLM API. The key design
choice: the bot reads the *same* `Connectivity` state machine as the sync
engine (ADR-002), so the bot's view of online/offline can never disagree
with the sync engine's. S6 shows offline answers and the bot tracking
OFFLINE → ONLINE transitions.

**Q: What if the LLM API fails mid-session while online?**
A: Graceful degradation: the bot falls back to offline intents and says
so, instead of erroring (ADR-002). The online path is real now — a
stdlib-only OpenAI-compatible `/chat/completions` client
(`offlinepos/llm.py`, no SDK dependency), configurable for OpenAI, any
OpenAI-compatible provider, or a local Ollama via
`OFFLINEPOS_LLM_BASE_URL`. Every failure mode (no key, timeout, HTTP
error, malformed JSON) raises `LLMError`, which the chatbot catches and
degrades on. Without `OFFLINEPOS_LLM_API_KEY` set, the bot simply stays on
offline intents (Known Limitations #6). S12 proves the live call and the
fallback against a fake server.

**Q: Doesn't an LLM make up numbers? How do you keep its answers factual?**
A: The system prompt injects live store context — today's sales, product
count, low-stock list, pending sync counts, last sync time — so answers
are grounded in the terminal's own data, and the prompt tells it to say
what it doesn't know rather than invent numbers.

## Billing / tax questions

**Q: How does tax calculation work?**
A: Per-product `tax_rate` (percent) on the catalog, applied exclusive of
price: `line_tax = round(price × qty × rate / 100, 2)` per line, computed
*inside* the same `BEGIN IMMEDIATE` ACID transaction as the sale
(ADR-006) — the tax breakdown can never disagree with the recorded sale.
`transactions` stores `subtotal`, `tax_total`, and the grand `total`;
receipts (text, ESC/POS thermal, web modal) print all three. S11 proves
the math (2× Widget @18% + 1× Bolt @5% + tax-free = $25.00 + $3.70 =
$28.70), persistence, printing, sync carry-over, and clean migration of
pre-tax databases (default 0%).

**Q: Why isn't the tax rate synced like prices are?**
A: Honest scoping: `tax_rate` is catalog-seeded master data
(`catalog/products.csv`), not yet part of the LWW `product_updates` sync —
changing a rate means re-seeding (Known Limitations #9). Pull convergence
deliberately leaves the local rate untouched, so a sync can never clobber
it silently.

## Architecture honesty questions

**Q: Your central DB is MySQL/MS SQL in the docs, but the code uses SQLite. Isn't that cheating?**
A: It's a documented, swappable boundary (ADR-005). `CentralDB` exposes
exactly the narrow interface the sync engine needs (`apply_batch`,
`fetch_products`, …) on SQLite, and swapping in `mysql.connector`
touches only that module. All SQL in the sync contract is portable — no
SQLite-only tricks — so the architecture claim holds; only the driver is
deferred to Phase 2.

**Q: What doesn't the system do?**
A: Lead with Known Limitations — examiners reward this: no multi-region
replication, no payment-gateway integration, no merge UI for master-data
conflicts, terminals that stay offline indefinitely diverge until they
sync (no peer-to-peer gossip), security is baseline (login with
cashier/manager roles and audit logging, no encryption-at-rest yet),
tax rates are catalog-seeded rather than synced, and one terminal
serializes checkouts through the billing worker.

## Suggested 2-minute live demo

1. `python3 simulations/run_all.py` — narrate S1 (offline sale → sync),
   S4 (flaky link → exactly-once), S5 (billing lane never blocks).
2. Open `docs/ARCHITECTURE_DECISIONS.md` — point at ADR-001 and ADR-003;
   examiners love named, recorded decisions.
3. End on Known Limitations — say which one you'd fix first (merge UI for
   master-data conflicts, or NTP-guarded LWW) and why.
