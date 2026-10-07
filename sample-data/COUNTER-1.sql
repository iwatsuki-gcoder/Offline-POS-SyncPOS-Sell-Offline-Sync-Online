BEGIN TRANSACTION;
CREATE TABLE audit_log(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);
INSERT INTO "audit_log" VALUES(1,1.79126164369149518e+09,'COUNTER-1','sale','{"txn_id": "f8f9888c2b104f97b69ec4058df073bb", "total": 45.5, "items": [["W1", 2, 10.0], ["G1", 1, 25.5]]}');
INSERT INTO "audit_log" VALUES(2,1.79126164375877761e+09,'COUNTER-1','sale','{"txn_id": "c1c1d14f7a854a8082c2375e27229027", "total": 50.0, "items": [["S1", 10, 5.0]]}');
INSERT INTO "audit_log" VALUES(3,1.79126164392118024e+09,'COUNTER-1','sync','txns=2 deltas=3 updates=0 retries=0');
CREATE TABLE conflicts(
    conflict_id TEXT PRIMARY KEY,
    entity      TEXT NOT NULL,          -- 'product'
    entity_id   TEXT NOT NULL,
    local_value TEXT,
    remote_value TEXT,
    resolution  TEXT NOT NULL,          -- e.g. 'last-write-wins'
    resolved_at REAL NOT NULL
);
CREATE TABLE kv(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO "kv" VALUES('last_sync_ts','1791261643.9004972');
CREATE TABLE product_updates(
    update_id  TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    price      REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE products(
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,          -- unix timestamp (see clock skew notes)
    updated_by  TEXT NOT NULL           -- terminal id, tie-breaker for LWW
, barcode TEXT);
INSERT INTO "products" VALUES('W1','Widget',10.0,18,1,1.79126164354405856e+09,'seed','8901011000015');
INSERT INTO "products" VALUES('G1','Gadget',25.5,14,1,1.7912616435924015e+09,'seed','8901011000022');
INSERT INTO "products" VALUES('S1','Sprocket',5.0,90,1,1.79126164361287879e+09,'seed','8901011000039');
INSERT INTO "products" VALUES('B1','Bolt',2.0,30,1,1.79126164363328957e+09,'seed','8901011000046');
INSERT INTO "products" VALUES('N1','Nut',1.0,50,1,1.79126164365422868e+09,'seed','8901011000053');
INSERT INTO "products" VALUES('Z1','Gizmo',12.75,40,1,1.7912616436735084e+09,'seed','8901011000060');
CREATE TABLE stock_deltas(
    delta_id    TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    delta       INTEGER NOT NULL,        -- negative = sale, positive = restock
    txn_id      TEXT,
    created_at  REAL NOT NULL,
    terminal_id TEXT NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "stock_deltas" VALUES('6e31e8c5cb4f47feaa47ee7fcb4979d3','W1',-2,'f8f9888c2b104f97b69ec4058df073bb',1.79126164369149518e+09,'COUNTER-1',1);
INSERT INTO "stock_deltas" VALUES('9d831c9d6b004ca785935ac88f0db76f','G1',-1,'f8f9888c2b104f97b69ec4058df073bb',1.79126164369149518e+09,'COUNTER-1',1);
INSERT INTO "stock_deltas" VALUES('fed307b921f7477a82e46edb60d39684','S1',-10,'c1c1d14f7a854a8082c2375e27229027',1.79126164375877761e+09,'COUNTER-1',1);
CREATE TABLE transactions(
    txn_id          TEXT PRIMARY KEY,
    terminal_id     TEXT NOT NULL,
    items_json      TEXT NOT NULL,      -- [[product_id, qty, unit_price], ...]
    total           REAL NOT NULL,
    created_at      REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    synced          INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "transactions" VALUES('f8f9888c2b104f97b69ec4058df073bb','COUNTER-1','[["W1", 2, 10.0], ["G1", 1, 25.5]]',45.5,1.79126164369149518e+09,'COUNTER-1:f8f9888c2b104f97b69ec4058df073bb',1);
INSERT INTO "transactions" VALUES('c1c1d14f7a854a8082c2375e27229027','COUNTER-1','[["S1", 10, 5.0]]',50.0,1.79126164375877761e+09,'COUNTER-1:c1c1d14f7a854a8082c2375e27229027',1);
CREATE UNIQUE INDEX idx_products_barcode ON products(barcode);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('audit_log',3);
COMMIT;
