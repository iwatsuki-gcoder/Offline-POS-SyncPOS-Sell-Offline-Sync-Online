"""Local SQLite store for one OfflinePOS terminal.

Design notes (OS + DBMS concepts):
- WAL journal mode + ``BEGIN IMMEDIATE`` write transactions give crash-safe,
  ACID local writes even through power loss.
- Every mutation that must survive a sync round-trip is recorded twice:
  once as current state (``products``) and once as an immutable, replayable
  fact (``stock_deltas`` / ``product_updates`` / ``transactions``). The fact
  tables are what the sync engine ships; the state tables are what the
  cashier reads. This is the delta-sync pattern: only changes move.
- ``clock`` is injectable so chaos tests can simulate clock skew.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    tax_rate    REAL NOT NULL DEFAULT 0, -- percent, e.g. 18 = 18% tax
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,          -- unix timestamp (see clock skew notes)
    updated_by  TEXT NOT NULL           -- terminal id, tie-breaker for LWW
);
CREATE TABLE IF NOT EXISTS transactions(
    txn_id          TEXT PRIMARY KEY,
    terminal_id     TEXT NOT NULL,
    items_json      TEXT NOT NULL,      -- [[product_id, qty, unit_price, tax_rate, line_tax], ...]
    total           REAL NOT NULL,      -- grand total incl. tax (kept for back-compat)
    subtotal        REAL NOT NULL DEFAULT 0, -- pre-tax
    tax_total       REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    synced          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stock_deltas(
    delta_id    TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    delta       INTEGER NOT NULL,        -- negative = sale, positive = restock
    txn_id      TEXT,
    created_at  REAL NOT NULL,
    terminal_id TEXT NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS product_updates(
    update_id  TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    price      REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);
CREATE TABLE IF NOT EXISTS conflicts(
    conflict_id TEXT PRIMARY KEY,
    entity      TEXT NOT NULL,          -- 'product'
    entity_id   TEXT NOT NULL,
    local_value TEXT,
    remote_value TEXT,
    resolution  TEXT NOT NULL,          -- e.g. 'last-write-wins'
    resolved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LocalDB:
    """Thread-safe wrapper around a terminal's local SQLite database."""

    def __init__(self, path: str | Path, clock=None):
        self.path = str(path)
        self.clock = clock or time.time
        self._local = threading.local()
        init = sqlite3.connect(self.path, timeout=30.0)
        init.execute("PRAGMA journal_mode=WAL;")
        init.execute("PRAGMA synchronous=NORMAL;")
        init.executescript(SCHEMA)
        # migration: barcodes were added after the first schema version
        try:
            init.execute("ALTER TABLE products ADD COLUMN barcode TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        init.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode "
                     "ON products(barcode)")
        # migration: per-product tax rates + sale tax breakdown
        for ddl in ("ALTER TABLE products ADD COLUMN tax_rate REAL NOT NULL DEFAULT 0",
                    "ALTER TABLE transactions ADD COLUMN subtotal REAL NOT NULL DEFAULT 0",
                    "ALTER TABLE transactions ADD COLUMN tax_total REAL NOT NULL DEFAULT 0"):
            try:
                init.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        init.commit()
        init.close()

    # -- low-level ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL;")
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    @contextmanager
    def write_txn(self):
        """ACID write transaction. ``BEGIN IMMEDIATE`` serialises writers."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def now(self) -> float:
        return self.clock()

    # -- products ----------------------------------------------------------
    def seed_product(self, product_id, name, price, stock, terminal_id="seed",
                     barcode=None, tax_rate=0.0):
        with self.write_txn() as c:
            c.execute(
                """INSERT INTO products(product_id,name,price,stock,tax_rate,version,updated_at,updated_by,barcode)
                   VALUES(?,?,?,?,?,?, ?, ?, ?)
                   ON CONFLICT(product_id) DO UPDATE SET
                     name=excluded.name, price=excluded.price, stock=excluded.stock,
                     tax_rate=excluded.tax_rate,
                     version=products.version+1, updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by, barcode=excluded.barcode""",
                (product_id, name, price, stock, float(tax_rate), 1, self.now(),
                 terminal_id, barcode),
            )

    def get_product(self, product_id) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_product_by_barcode(self, barcode: str) -> dict | None:
        """What the scanner calls: barcode -> product."""
        row = self._conn().execute(
            "SELECT * FROM products WHERE barcode=?", (barcode.strip(),)
        ).fetchone()
        return dict(row) if row else None

    def list_products(self) -> list[dict]:
        rows = self._conn().execute("SELECT * FROM products ORDER BY product_id").fetchall()
        return [dict(r) for r in rows]

    def update_product_price(self, product_id, new_price, terminal_id, ts=None) -> dict:
        """Master-data change. Recorded as a replayable update for LWW merge."""
        ts = self.now() if ts is None else ts
        prod = self.get_product(product_id)
        if not prod:
            raise KeyError(f"unknown product {product_id}")
        update_id = uuid.uuid4().hex
        with self.write_txn() as c:
            c.execute(
                """INSERT INTO product_updates(update_id,product_id,name,price,updated_at,updated_by,synced)
                   VALUES(?,?,?,?,?,?,0)""",
                (update_id, product_id, prod["name"], new_price, ts, terminal_id),
            )
            c.execute(
                """UPDATE products SET price=?, version=version+1, updated_at=?, updated_by=?
                   WHERE product_id=?""",
                (new_price, ts, terminal_id, product_id),
            )
            c.execute(
                "INSERT INTO audit_log(ts,actor,action,details) VALUES(?,?,?,?)",
                (self.now(), terminal_id, "price_update",
                 json.dumps({"product_id": product_id, "new_price": new_price})),
            )
        return {"update_id": update_id, "product_id": product_id,
                "price": new_price, "updated_at": ts, "updated_by": terminal_id}

    # -- sales (the billing hot path) --------------------------------------
    def create_sale(self, items: list[tuple[str, int]], terminal_id: str) -> dict:
        """Atomically: validate stock, decrement, record txn + deltas.

        One ``BEGIN IMMEDIATE`` transaction => the sale is all-or-nothing
        (ACID). Tax is computed per line (``line_tax = round(price*qty*rate/100,
        2)``) inside the same transaction, so the tax breakdown can never
        disagree with the recorded sale. Each line item also appends a stock
        delta so offline sales from many terminals commute and merge without
        conflicts.
        """
        txn_id = uuid.uuid4().hex
        idem = f"{terminal_id}:{txn_id}"
        created = self.now()
        with self.write_txn() as c:
            lines, subtotal, tax_total = [], 0.0, 0.0
            for product_id, qty in items:
                row = c.execute(
                    "SELECT price, stock, tax_rate FROM products WHERE product_id=?",
                    (product_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown product {product_id}")
                price, stock = row["price"], row["stock"]
                tax_rate = row["tax_rate"] or 0.0
                if stock < qty:
                    raise ValueError(
                        f"insufficient stock for {product_id}: have {stock}, need {qty}")
                line_sub = price * qty
                line_tax = round(line_sub * tax_rate / 100.0, 2)
                subtotal += line_sub
                tax_total += line_tax
                lines.append([product_id, qty, price, tax_rate, line_tax])
                c.execute("UPDATE products SET stock=stock-? WHERE product_id=?",
                          (qty, product_id))
                c.execute(
                    """INSERT INTO stock_deltas(delta_id,product_id,delta,txn_id,created_at,terminal_id,synced)
                       VALUES(?,?,?,?,?,?,0)""",
                    (uuid.uuid4().hex, product_id, -qty, txn_id, created, terminal_id),
                )
            subtotal = round(subtotal, 2)
            tax_total = round(tax_total, 2)
            total = round(subtotal + tax_total, 2)
            c.execute(
                """INSERT INTO transactions(txn_id,terminal_id,items_json,total,subtotal,tax_total,
                                            created_at,idempotency_key,synced)
                   VALUES(?,?,?,?,?,?,?,?,0)""",
                (txn_id, terminal_id, json.dumps(lines), total, subtotal,
                 tax_total, created, idem),
            )
            c.execute(
                "INSERT INTO audit_log(ts,actor,action,details) VALUES(?,?,?,?)",
                (created, terminal_id, "sale",
                 json.dumps({"txn_id": txn_id, "subtotal": subtotal,
                             "tax_total": tax_total, "total": total,
                             "items": lines})),
            )
        return {"txn_id": txn_id, "idempotency_key": idem,
                "subtotal": subtotal, "tax_total": tax_total, "total": total,
                "items": lines, "created_at": created, "terminal_id": terminal_id}

    # -- sync cursors -------------------------------------------------------
    def pending_txns(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM transactions WHERE synced=0 ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def pending_deltas(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM stock_deltas WHERE synced=0 ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def pending_updates(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM product_updates WHERE synced=0 ORDER BY updated_at").fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, table: str, id_col: str, ids: list[str]):
        if not ids:
            return
        with self.write_txn() as c:
            c.executemany(
                f"UPDATE {table} SET synced=1 WHERE {id_col}=?",
                [(i,) for i in ids],
            )

    def pending_counts(self) -> dict:
        c = self._conn()
        return {
            "txns": c.execute("SELECT COUNT(*) n FROM transactions WHERE synced=0").fetchone()["n"],
            "deltas": c.execute("SELECT COUNT(*) n FROM stock_deltas WHERE synced=0").fetchone()["n"],
            "updates": c.execute("SELECT COUNT(*) n FROM product_updates WHERE synced=0").fetchone()["n"],
        }

    # -- pull (central -> local convergence) --------------------------------
    def apply_central_products(self, products: list[dict]):
        """Converge local state toward central after a push.

        Skips products with unsynced local master-data updates (they will win
        or lose on the next push via LWW) and skips stock convergence while
        local deltas are still pending, so we never clobber unpushed sales.
        """
        with self.write_txn() as c:
            for p in products:
                pend_upd = c.execute(
                    "SELECT COUNT(*) n FROM product_updates WHERE product_id=? AND synced=0",
                    (p["product_id"],)).fetchone()["n"]
                pend_delta = c.execute(
                    "SELECT COUNT(*) n FROM stock_deltas WHERE product_id=? AND synced=0",
                    (p["product_id"],)).fetchone()["n"]
                if pend_upd or pend_delta:
                    continue
                c.execute(
                    """INSERT INTO products(product_id,name,price,stock,version,updated_at,updated_by)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(product_id) DO UPDATE SET
                         name=excluded.name, price=excluded.price, stock=excluded.stock,
                         version=excluded.version, updated_at=excluded.updated_at,
                         updated_by=excluded.updated_by""",
                    (p["product_id"], p["name"], p["price"], p["stock"],
                     p["version"], p["updated_at"], p["updated_by"]),
                )

    # -- conflicts / kv / reporting -----------------------------------------
    def record_conflict(self, entity, entity_id, local_value, remote_value, resolution):
        with self.write_txn() as c:
            c.execute(
                """INSERT INTO conflicts(conflict_id,entity,entity_id,local_value,remote_value,
                                         resolution,resolved_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, entity, entity_id,
                 json.dumps(local_value), json.dumps(remote_value),
                 resolution, self.now()),
            )

    def list_conflicts(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM conflicts ORDER BY resolved_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["local_value"] = json.loads(d["local_value"])
            d["remote_value"] = json.loads(d["remote_value"])
            out.append(d)
        return out

    def set_kv(self, key, value):
        with self.write_txn() as c:
            c.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    def get_kv(self, key, default=None):
        row = self._conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def sales_total_since(self, since_ts: float) -> float:
        row = self._conn().execute(
            "SELECT COALESCE(SUM(total),0) s FROM transactions WHERE created_at>=?",
            (since_ts,)).fetchone()
        return round(row["s"], 2)

    def audit(self, actor, action, details=""):
        with self.write_txn() as c:
            c.execute("INSERT INTO audit_log(ts,actor,action,details) VALUES(?,?,?,?)",
                      (self.now(), actor, action, details))
