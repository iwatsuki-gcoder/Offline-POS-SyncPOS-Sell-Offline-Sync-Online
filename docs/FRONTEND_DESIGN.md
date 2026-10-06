# Frontend Design — OfflinePOS

> **Built.** The wireframe became a working app: `web/backend/app.py`
> (FastAPI) + `web/frontend/index.html` (single-file UI, no build step).
> Run with `uvicorn web.backend.app:app --port 8000`, open
> http://localhost:8000. The clickable mockup below is still useful as a
> design reference: open `docs/frontend_mockup.html` in a browser.

## Screens

### 1. Billing (the cashier screen — default)
- **Top bar:** terminal id (`COUNTER-1`), connectivity pill (green ONLINE /
  red OFFLINE — bound to the *same* state the sync engine uses), clock.
- **Left:** product grid with search box + category filter. Each card shows
  name, price, live stock; out-of-stock cards are greyed out and
  un-tappable.
- **Right:** cart panel — line items with qty steppers, running total,
  big **Charge** button. On success: receipt modal (mirrors
  `Terminal._receipt`), cart clears.
- **Rule:** the Charge button never disables because of sync state. Sales
  are local-first; sync is someone else's problem (the background lane).

### 2. Sync dashboard (manager view)
- Connectivity status + **Sync now** button (manual trigger).
- Pending counters: transactions / stock deltas / product updates, with
  "last sync" timestamp.
- Conflicts table: entity, local vs remote value, resolution, time —
  read from the local `conflicts` table; a "reviewed" checkbox for the
  manager (maps to a future `reviewed` flag).
- Product table: price, local stock, version, last-updated-by.

### 3. Assistant (chat panel, docked right or full-screen on tablet)
- Chat bubbles, quick-suggestion chips: *Sales today*, *Stock of …*,
  *Sync status*, *Help*.
- Mode badge: **Offline** (local intents) vs **Online** (LLM). When online
  but the LLM key isn't configured, the badge reads *Online (local mode)*
  and the bot says so — never a spinner of doom.

## User flows
- **Sale offline:** scan → cart → Charge → receipt prints → deltas queue →
  pill flips ONLINE later → auto-sync → pending counters drain to zero.
- **Conflict:** two counters edit a price offline → both sync → dashboard
  conflicts table gains a row → manager reviews.
- **Assistant:** cashier asks "why didn't yesterday sync?" offline → bot
  answers from `audit_log`/pending counts; online → LLM with the same
  local context injected.

## Design tokens (suggested)
- Theme: dark POS theme (low glare at counters). Background `#121417`,
  surface `#1c1f24`, text `#f2f4f8`, muted `#9aa3b2`.
- Accent: green `#22c55e` (online/success), red `#ef4444` (offline/error),
  amber `#f59e0b` (pending/conflict), brand blue `#3b82f6` (primary buttons).
- Font: system stack or Inter; tabular numerals for prices/totals.
- Touch targets ≥ 48px (cashiers tap fast).

## Tech recommendation (for the later build)
- **Web UI** (runs on tablets/phones/desktops): FastAPI backend exposing
  the API below + a lightweight frontend (plain JS or React). The Python
  `Terminal` class becomes the service layer; the browser never touches
  SQLite directly.
- Alternative: PyQt desktop app if you want a single native binary.
  The web route is recommended — same UI on every device.

## Auth & roles (added)
- Login screen on load; PBKDF2-hashed passwords, bearer-token sessions.
- `cashier`: Billing + Assistant tabs only.
- `manager`: adds Sync Dashboard (sync control, conflicts, price changes,
  demo buttons).
- Demo accounts: `manager/admin123`, `cashier/cashier123`.

## Receipt printing (added)
- Receipt modal has a **Print receipt** button → `POST /api/receipt/print`.
- File backend (default): 42-column text receipt saved under
  `data/receipts/` — works with zero hardware.
- Network backend: ESC/POS byte stream to a thermal printer
  (`OFFLINEPOS_PRINTER_HOST:PORT`); unreachable printer falls back to
  file, never fails the sale.

## API contract the frontend will need (sketch)
| Method | Route | Maps to |
|---|---|---|
| GET | `/api/products` | `LocalDB.list_products` |
| POST | `/api/cart/checkout` | `Terminal.checkout` (BILLING lane) |
| GET | `/api/sync/status` | `pending_counts` + `last_sync_ts` + net state |
| POST | `/api/sync/now` | `Terminal.sync_now` |
| GET | `/api/conflicts` | `LocalDB.list_conflicts` |
| POST | `/api/chat/ask` `{question}` | `Terminal.ask` |
| GET | `/api/health` | `render_dashboard` data |

All endpoints are terminal-local; nothing here requires the central DB
except through the sync engine.
