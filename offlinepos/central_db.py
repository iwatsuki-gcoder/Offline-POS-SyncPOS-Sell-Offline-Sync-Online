"""Central database backed by MySQL (or MariaDB).

Drop-in replacement for the old SQLite simulation: same narrow interface the
sync engine needs (``apply_batch`` / ``fetch_products``) plus the helpers the
web app uses (``seed_product`` / ``get_product`` / ``txn_count``).

Conflict strategy (unchanged, see docs/ARCHITECTURE_DECISIONS.md):
- Stock: **delta merge**. Sales/restocks are commutative facts
  (``stock = stock + delta``), applied exactly once via ``applied_deltas``.
- Product master data (name/price): **last-write-wins** ordered by
  ``(updated_at, updated_by)``. The loser is reported back to the terminal.
- Transactions: **idempotent apply**. ``idempotency_key`` is UNIQUE; a
  retried push is a no-op, never a duplicate sale.

MySQL-specific behaviour
- Connection settings come from the environment (see ``from_env``).
- The database, tables and indexes are created automatically on first use.
- Connections are lazy: constructing ``CentralDB`` never touches the network,
  so a terminal can boot and keep billing while the central server is down.
- Connectivity failures are re-raised as ``ConnectionError``. That is exactly
  what ``SyncEngine`` retries with exponential backoff; the batch is rolled
  back, and the idempotency keys make a retry safe even if a COMMIT succeeded
  but its acknowledgement was lost.
- Row locks (``SELECT ... FOR UPDATE``) serialize concurrent terminals on the
  same product; deadlocks (errno 1213) and lock timeouts (1205) are retried.

Environment
    OFFLINEPOS_MYSQL_HOST       default 127.0.0.1
    OFFLINEPOS_MYSQL_PORT       default 3306
    OFFLINEPOS_MYSQL_USER       default offlinepos
    OFFLINEPOS_MYSQL_PASSWORD   default (empty)
    OFFLINEPOS_MYSQL_DATABASE   default offlinepos_central
    OFFLINEPOS_MYSQL_POOL       default 5
    OFFLINEPOS_MYSQL_TIMEOUT    connect timeout in seconds, default 5
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling

_TABLE_OPTS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS products(
        product_id  VARCHAR(64)  NOT NULL PRIMARY KEY,
        name        VARCHAR(255) NOT NULL,
        price       DOUBLE       NOT NULL,
        stock       INT          NOT NULL DEFAULT 0,
        tax_rate    DOUBLE       NOT NULL DEFAULT 0,
        version     INT          NOT NULL DEFAULT 1,
        updated_at  DOUBLE       NOT NULL,
        updated_by  VARCHAR(64)  NOT NULL,
        barcode     VARCHAR(32)  NULL,
        UNIQUE KEY idx_products_barcode (barcode)
    ) {_TABLE_OPTS}""",
    f"""CREATE TABLE IF NOT EXISTS transactions(
        txn_id          VARCHAR(64)  NOT NULL PRIMARY KEY,
        terminal_id     VARCHAR(64)  NOT NULL,
        items_json      LONGTEXT     NOT NULL,
        total           DOUBLE       NOT NULL,
        created_at      DOUBLE       NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL,
        UNIQUE KEY uq_txn_idempotency (idempotency_key),
        KEY idx_txn_terminal (terminal_id, created_at)
    ) {_TABLE_OPTS}""",
    f"""CREATE TABLE IF NOT EXISTS applied_deltas(
        delta_id VARCHAR(64) NOT NULL PRIMARY KEY
    ) {_TABLE_OPTS}""",
    f"""CREATE TABLE IF NOT EXISTS central_audit(
        id      BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
        ts      DOUBLE       NOT NULL,
        action  VARCHAR(64)  NOT NULL,
        details TEXT         NULL
    ) {_TABLE_OPTS}""",
]

_RESET_ORDER = ("applied_deltas", "transactions", "products", "central_audit")

# 1213 deadlock, 1205 lock wait timeout -> safe to retry the whole transaction
_RETRYABLE_ERRNOS = {1213, 1205}
# server unreachable / went away / too many connections
_CONNECTION_ERRNOS = {1040, 1053, 2002, 2003, 2006, 2013, 2055}


def _is_connection_error(e: Exception) -> bool:
    if isinstance(e, (mysql.connector.InterfaceError, pooling.PoolError)):
        return True
    return getattr(e, "errno", None) in _CONNECTION_ERRNOS


class CentralDB:
    def __init__(self, host: str = "127.0.0.1", port: int = 3306,
                 user: str = "offlinepos", password: str = "",
                 database: str = "offlinepos_central", pool_size: int = 5,
                 connect_timeout: int = 5, deadlock_retries: int = 3):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
            raise ValueError("database name may only contain letters, "
                             "digits and underscores")
        self.database = database
        self.deadlock_retries = deadlock_retries
        self._pool_size = max(1, min(int(pool_size), 32))
        self._cfg = dict(host=host, port=int(port), user=user,
                         password=password, charset="utf8mb4",
                         connection_timeout=connect_timeout)
        self._pool: pooling.MySQLConnectionPool | None = None
        self._init_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "CentralDB":
        env = os.environ.get
        return cls(
            host=env("OFFLINEPOS_MYSQL_HOST", "127.0.0.1"),
            port=int(env("OFFLINEPOS_MYSQL_PORT", "3306")),
            user=env("OFFLINEPOS_MYSQL_USER", "offlinepos"),
            password=env("OFFLINEPOS_MYSQL_PASSWORD", ""),
            database=env("OFFLINEPOS_MYSQL_DATABASE", "offlinepos_central"),
            pool_size=int(env("OFFLINEPOS_MYSQL_POOL", "5")),
            connect_timeout=int(env("OFFLINEPOS_MYSQL_TIMEOUT", "5")),
        )

    # -- connection management --------------------------------------------------
    def _ensure_ready(self):
        """Create the database/schema and the pool on first use (lazy)."""
        if self._pool is not None:
            return
        with self._init_lock:
            if self._pool is not None:
                return
            try:
                boot = mysql.connector.connect(**self._cfg)
                try:
                    cur = boot.cursor()
                    try:
                        cur.execute(
                            f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                            "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
                    except mysql.connector.Error:
                        pass  # no CREATE privilege; the DB may already exist
                    finally:
                        cur.close()
                finally:
                    boot.close()

                pool = pooling.MySQLConnectionPool(
                    pool_name=f"offlinepos_{id(self)}",
                    pool_size=self._pool_size,
                    database=self.database, **self._cfg)
                conn = pool.get_connection()
                try:
                    cur = conn.cursor()
                    for stmt in SCHEMA:
                        cur.execute(stmt)
                    conn.commit()
                    cur.close()
                finally:
                    conn.close()
            except (mysql.connector.Error, OSError) as e:
                raise ConnectionError(f"central MySQL unavailable: {e}") from e
            self._pool = pool

    @contextmanager
    def _connection(self):
        self._ensure_ready()
        assert self._pool is not None
        try:
            conn = self._pool.get_connection()
            conn.ping(reconnect=True, attempts=2, delay=0)
        except (mysql.connector.Error, OSError) as e:
            raise ConnectionError(f"central MySQL unavailable: {e}") from e
        try:
            yield conn
        finally:
            conn.close()  # returns the connection to the pool

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connection() as c:
            cur = c.cursor(dictionary=True, buffered=True)
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
                c.commit()  # end the read snapshot
                return rows
            except mysql.connector.Error as e:
                if _is_connection_error(e):
                    raise ConnectionError(f"central MySQL unavailable: {e}") from e
                raise
            finally:
                cur.close()

    # -- admin / seed -------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._query("SELECT 1 AS ok")
            return True
        except ConnectionError:
            return False

    def reset(self):
        """Empty every table (demo reset / tests). Destructive."""
        with self._connection() as c:
            cur = c.cursor()
            try:
                for table in _RESET_ORDER:
                    cur.execute(f"TRUNCATE TABLE {table}")
                c.commit()
            except mysql.connector.Error as e:
                if _is_connection_error(e):
                    raise ConnectionError(f"central MySQL unavailable: {e}") from e
                raise
            finally:
                cur.close()

    def seed_product(self, product_id, name, price, stock, barcode=None,
                     tax_rate=0.0):
        with self._connection() as c:
            cur = c.cursor()
            try:
                cur.execute(
                    """INSERT INTO products
                         (product_id,name,price,stock,tax_rate,version,
                          updated_at,updated_by,barcode)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                         name=VALUES(name), price=VALUES(price),
                         stock=VALUES(stock), tax_rate=VALUES(tax_rate),
                         barcode=VALUES(barcode)""",
                    (product_id, name, price, stock, float(tax_rate), 1,
                     time.time(), "seed", barcode))
                c.commit()
            except mysql.connector.Error as e:
                c.rollback()
                if _is_connection_error(e):
                    raise ConnectionError(f"central MySQL unavailable: {e}") from e
                raise
            finally:
                cur.close()

    # -- the sync interface ---------------------------------------------------------
    def apply_batch(self, product_updates=(), deltas=(), txns=()) -> dict:
        """Apply one terminal's delta batch. Atomic; safe to retry."""
        # Stable lock order across concurrent terminals avoids most deadlocks.
        updates = sorted(product_updates,
                         key=lambda u: (u["product_id"], u["updated_at"],
                                        u["updated_by"]))
        deltas = sorted(deltas, key=lambda d: d["product_id"])
        txns = list(txns)

        for attempt in range(self.deadlock_retries + 1):
            try:
                return self._apply_once(updates, deltas, txns)
            except mysql.connector.Error as e:
                if (getattr(e, "errno", None) in _RETRYABLE_ERRNOS
                        and attempt < self.deadlock_retries):
                    time.sleep(0.05 * (attempt + 1) + random.uniform(0, 0.05))
                    continue
                if _is_connection_error(e):
                    raise ConnectionError(
                        f"central MySQL unavailable: {e}") from e
                raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _apply_once(self, updates, deltas, txns) -> dict:
        report = {"applied_txns": 0, "deduped_txns": 0, "applied_deltas": 0,
                  "deduped_deltas": 0, "applied_updates": 0, "conflicts": []}
        with self._connection() as c:
            cur = c.cursor(dictionary=True, buffered=True)
            try:
                c.start_transaction()

                # 1) master-data updates: LWW on (updated_at, updated_by)
                for u in updates:
                    cur.execute(
                        "SELECT * FROM products WHERE product_id=%s FOR UPDATE",
                        (u["product_id"],))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            """INSERT INTO products
                                 (product_id,name,price,stock,tax_rate,version,
                                  updated_at,updated_by)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (u["product_id"], u["name"], u["price"], 0, 0.0, 1,
                             u["updated_at"], u["updated_by"]))
                        report["applied_updates"] += 1
                        continue
                    incoming = (u["updated_at"], u["updated_by"])
                    stored = (row["updated_at"], row["updated_by"])
                    differs = (u["price"] != row["price"]) or (u["name"] != row["name"])
                    if incoming >= stored:
                        if differs:
                            report["conflicts"].append({
                                "entity": "product", "entity_id": u["product_id"],
                                "winner": dict(u), "loser": dict(row),
                                "resolution": "last-write-wins (incoming newer)",
                            })
                        cur.execute(
                            """UPDATE products
                                  SET name=%s, price=%s, version=version+1,
                                      updated_at=%s, updated_by=%s
                                WHERE product_id=%s""",
                            (u["name"], u["price"], u["updated_at"],
                             u["updated_by"], u["product_id"]))
                        report["applied_updates"] += 1
                    elif differs:
                        report["conflicts"].append({
                            "entity": "product", "entity_id": u["product_id"],
                            "winner": dict(row), "loser": dict(u),
                            "resolution":
                                "last-write-wins (central kept, incoming stale)",
                        })

                # 2) stock deltas: commutative merge, exactly-once
                for d in deltas:
                    cur.execute(
                        "INSERT IGNORE INTO applied_deltas(delta_id) VALUES(%s)",
                        (d["delta_id"],))
                    if cur.rowcount == 0:
                        report["deduped_deltas"] += 1
                        continue
                    cur.execute(
                        "UPDATE products SET stock=stock+%s WHERE product_id=%s",
                        (d["delta"], d["product_id"]))
                    report["applied_deltas"] += 1

                # 3) transactions: idempotent by key
                for t in txns:
                    cur.execute(
                        """INSERT IGNORE INTO transactions
                             (txn_id,terminal_id,items_json,total,created_at,
                              idempotency_key)
                           VALUES(%s,%s,%s,%s,%s,%s)""",
                        (t["txn_id"], t["terminal_id"], t["items_json"],
                         t["total"], t["created_at"], t["idempotency_key"]))
                    if cur.rowcount == 0:
                        report["deduped_txns"] += 1
                    else:
                        report["applied_txns"] += 1

                cur.execute(
                    "INSERT INTO central_audit(ts,action,details) VALUES(%s,%s,%s)",
                    (time.time(), "apply_batch",
                     f"txns={report['applied_txns']}/{report['deduped_txns']} "
                     f"deltas={report['applied_deltas']}/{report['deduped_deltas']}"))
                c.commit()
            except BaseException:
                try:
                    c.rollback()
                except Exception:
                    pass  # connection already dead; server rolls back itself
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        return report

    # -- reads ----------------------------------------------------------------------
    def fetch_products(self) -> list[dict]:
        return self._query("SELECT * FROM products ORDER BY product_id")

    def txn_count(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM transactions")[0]["n"])

    def get_product(self, product_id) -> dict | None:
        rows = self._query("SELECT * FROM products WHERE product_id=%s",
                           (product_id,))
        return rows[0] if rows else None