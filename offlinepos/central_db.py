"""Simulated central database.

In production this is MySQL / MS SQL Server. The class exposes the same
narrow interface the sync engine needs (``apply_batch`` / ``fetch_products``),
so swapping the SQLite file below for ``mysql.connector`` is a contained
change: only this module is touched.

Conflict strategy (documented, see docs/ARCHITECTURE_DECISIONS.md):
- Stock: **delta merge**. Sales/restocks are commutative facts
  (``stock = stock + delta``), so concurrent offline terminals never
  conflict on inventory. Deltas are applied exactly once via
  ``applied_deltas``.
- Product master data (name/price): **last-write-wins** ordered by
  ``(updated_at, updated_by)``. The loser is recorded and reported so the
  terminal can log it for manual review.
- Transactions: **idempotent apply**. ``idempotency_key`` is UNIQUE; a
  retried push is a no-op, never a duplicate sale.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,
    updated_by  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions(
    txn_id          TEXT PRIMARY KEY,
    terminal_id     TEXT NOT NULL,
    items_json      TEXT NOT NULL,
    total           REAL NOT NULL,
    created_at      REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS applied_deltas(
    delta_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS central_audit(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);
"""


class CentralDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        init = sqlite3.connect(self.path, timeout=30.0)
        init.execute("PRAGMA journal_mode=WAL;")
        init.executescript(SCHEMA)
        init.commit()
        init.close()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    # -- seed (test/setup only) ---------------------------------------------
    def seed_product(self, product_id, name, price, stock):
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO products(product_id,name,price,stock,version,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                     name=excluded.name, price=excluded.price, stock=excluded.stock""",
                (product_id, name, price, stock, 1, time.time(), "seed"),
            )

    # -- the sync interface ---------------------------------------------------
    def apply_batch(self, product_updates=(), deltas=(), txns=()) -> dict:
        """Apply one terminal's delta batch. Atomic; safe to retry."""
        report = {"applied_txns": 0, "deduped_txns": 0, "applied_deltas": 0,
                  "deduped_deltas": 0, "applied_updates": 0, "conflicts": []}
        with self._lock:
            c = self._conn()
            try:
                c.execute("BEGIN IMMEDIATE")
                # 1) master-data updates: LWW on (updated_at, updated_by)
                for u in product_updates:
                    cur = c.execute("SELECT * FROM products WHERE product_id=?",
                                    (u["product_id"],)).fetchone()
                    if cur is None:
                        c.execute(
                            """INSERT INTO products(product_id,name,price,stock,version,updated_at,updated_by)
                               VALUES(?,?,?,?,?,?,?)""",
                            (u["product_id"], u["name"], u["price"], 0, 1,
                             u["updated_at"], u["updated_by"]))
                        report["applied_updates"] += 1
                        continue
                    incoming = (u["updated_at"], u["updated_by"])
                    stored = (cur["updated_at"], cur["updated_by"])
                    differs = (u["price"] != cur["price"]) or (u["name"] != cur["name"])
                    if incoming >= stored:
                        if differs:
                            report["conflicts"].append({
                                "entity": "product", "entity_id": u["product_id"],
                                "winner": dict(u), "loser": dict(cur),
                                "resolution": "last-write-wins (incoming newer)",
                            })
                        c.execute(
                            """UPDATE products SET name=?, price=?, version=version+1,
                                                  updated_at=?, updated_by=? WHERE product_id=?""",
                            (u["name"], u["price"], u["updated_at"],
                             u["updated_by"], u["product_id"]))
                        report["applied_updates"] += 1
                    else:
                        if differs:
                            report["conflicts"].append({
                                "entity": "product", "entity_id": u["product_id"],
                                "winner": dict(cur), "loser": dict(u),
                                "resolution": "last-write-wins (central kept, incoming stale)",
                            })
                # 2) stock deltas: commutative merge, exactly-once
                for d in deltas:
                    cur = c.execute("INSERT OR IGNORE INTO applied_deltas(delta_id) VALUES(?)",
                                    (d["delta_id"],))
                    if cur.rowcount == 0:
                        report["deduped_deltas"] += 1
                        continue
                    c.execute("UPDATE products SET stock=stock+? WHERE product_id=?",
                              (d["delta"], d["product_id"]))
                    report["applied_deltas"] += 1
                # 3) transactions: idempotent by key
                for t in txns:
                    cur = c.execute(
                        """INSERT OR IGNORE INTO transactions
                           (txn_id,terminal_id,items_json,total,created_at,idempotency_key)
                           VALUES(?,?,?,?,?,?)""",
                        (t["txn_id"], t["terminal_id"], t["items_json"], t["total"],
                         t["created_at"], t["idempotency_key"]))
                    if cur.rowcount == 0:
                        report["deduped_txns"] += 1
                    else:
                        report["applied_txns"] += 1
                c.execute(
                    "INSERT INTO central_audit(ts,action,details) VALUES(?,?,?)",
                    (time.time(), "apply_batch",
                     f"txns={report['applied_txns']}/{report['deduped_txns']} "
                     f"deltas={report['applied_deltas']}/{report['deduped_deltas']}"))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            finally:
                c.close()
        return report

    def fetch_products(self) -> list[dict]:
        with self._lock:
            c = self._conn()
            try:
                rows = c.execute("SELECT * FROM products ORDER BY product_id").fetchall()
                return [dict(r) for r in rows]
            finally:
                c.close()

    def txn_count(self) -> int:
        with self._lock:
            c = self._conn()
            try:
                return c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
            finally:
                c.close()

    def get_product(self, product_id) -> dict | None:
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM products WHERE product_id=?",
                                (product_id,)).fetchone()
                return dict(row) if row else None
            finally:
                c.close()
