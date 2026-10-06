# SwiftBill Web App

Runs on **macOS, Windows, and Linux** — anything with Python 3.10+ and a
web browser. No build step, no app store, no installers.

The server is designed to run **on the terminal itself** (`localhost`), so
the POS keeps working with no internet — the offline-first story is
preserved. The browser is just the display.

## Setup (all operating systems)

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# 2. Install dependencies
pip install -r web/requirements.txt

# 3. Start the server
uvicorn web.backend.app:app --host 127.0.0.1 --port 8000

# 4. Open http://localhost:8000
```

In VS Code: select the `.venv` interpreter, then press **F5 → "Run web app"**.

## What's inside

| Path | What |
|---|---|
| `web/backend/app.py` | FastAPI API over the existing engine (products, checkout, sync, chat, demos) |
| `web/frontend/index.html` | Single-file UI: Billing, Sync Dashboard, Assistant — no build tools needed |

Two terminals (`COUNTER-1`, `COUNTER-2`) are served; switch between them in
the header. Data lives in `./data/` (created on first run).

## Barcode scanning

Each product has an EAN-13 barcode (see `catalog/products.csv`, the source
of truth — the app seeds both terminal and central DBs from it). Three
ways to scan at the counter:

1. **Camera** — the 📷 Scan button uses the browser's native
   `BarcodeDetector` (no library, works offline; Chrome/Edge).
2. **USB barcode scanner** — these type digits + Enter, so scanning into
   the search box and hitting Enter adds the product straight to the cart.
3. **Manual** — type the barcode digits + Enter, or search by name.

Lookup: `GET /api/products/by-barcode/{code}` (404 if unknown).

## Login

Cashiers and managers sign in on the login screen:

| Username | Password | Role |
|---|---|---|
| `manager` | `admin123` | manager — everything |
| `cashier` | `cashier123` | cashier — billing + assistant only |

Passwords are PBKDF2-hashed; sessions are bearer tokens (8h). Change the
seed passwords before any real deployment (`AuthStore.create_user`).

## Receipt printing

Set via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `OFFLINEPOS_PRINTER` | `file` | `file` or `network` |
| `OFFLINEPOS_RECEIPT_DIR` | `./data/receipts` | where text receipts are saved |
| `OFFLINEPOS_PRINTER_HOST` | — | thermal printer IP (network mode) |
| `OFFLINEPOS_PRINTER_PORT` | `9100` | thermal printer port |

File mode works everywhere and doubles as an audit trail. Network mode
sends ESC/POS to a thermal printer and falls back to a file if the
printer is unreachable — it never crashes a sale.

Receipts print SUBTOTAL / TAX / TOTAL. Tax rates come from
`catalog/products.csv` (`tax_rate` column, percent, exclusive of price).

## Connectivity

The header pill is a **manual simulation toggle** by default (deterministic
offline/online for demos and chaos testing). Press **Auto ○** next to it
to switch to real connectivity auto-detect: the backend probes actual
internet reachability every 10 s and drives the pill, sync engine, and
chatbot from the result. Clicking the pill while in auto mode drops back
to manual.

| Variable | Default | Meaning |
|---|---|---|
| `OFFLINEPOS_NET_MODE` | `manual` | `auto` to start with real connectivity detection |

API: `POST /api/net` (manual set), `POST /api/net/mode`
(`{"mode":"auto"|"manual"}`), `GET /api/net/status`.

## Chatbot LLM (optional)

The Assistant tab answers from local data when offline. Online, it can
call a real LLM (OpenAI-compatible) — set these to enable it:

| Variable | Default | Meaning |
|---|---|---|
| `OFFLINEPOS_LLM_API_KEY` | — | required; without it the bot stays on offline intents |
| `OFFLINEPOS_LLM_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint, e.g. `http://localhost:11434/v1` for local Ollama |
| `OFFLINEPOS_LLM_MODEL` | `gpt-4o-mini` | model name |
| `OFFLINEPOS_LLM_TIMEOUT` | `20` | seconds per request |

Any failure (no key, timeout, bad response) falls back to offline intents
— the terminal never blocks on the network.

## API quick reference

- `GET /api/products`, `POST /api/cart/checkout`
- `GET /api/sync/status`, `POST /api/sync/now`, `POST /api/net`
- `GET /api/conflicts`, `GET /api/health`
- `POST /api/chat/ask`
- `POST /api/demo/conflict`, `POST /api/demo/reset`

Interactive docs: http://localhost:8000/docs
