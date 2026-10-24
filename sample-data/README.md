# Sample databases

Snapshots of the live SQLite databases, generated 2026-10-06 with
simulated activity (3 offline sales across 2 counters, then synced).

| File | What |
|---|---|
| `central.db` | Main database of the whole system: merged products, 3 transactions, applied deltas |
| `COUNTER-1.db` | Local database of counter 1 (2 sales) |
| `COUNTER-2.db` | Local database of counter 2 (1 sale + 1 price update) |
| `auth.db` | Login users (`manager` / `cashier`) |

Open with any SQLite viewer (e.g. the VS Code SQLite extension) or:

```bash
sqlite3 central.db "SELECT product_id, name, price, stock FROM products;"
```

These are snapshots — starting the web server or hitting "Reset demo data"
rebuilds them. Live runtime data is git-ignored under `data/`.
