BEGIN TRANSACTION;
CREATE TABLE audit_log(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);
INSERT INTO "audit_log" VALUES(1,1.79126164377877736e+09,'COUNTER-2','sale','{"txn_id": "fadf00d7e26e4a358cc2b930e65bc42f", "total": 18.0, "items": [["B1", 4, 2.0], ["N1", 10, 1.0]]}');
INSERT INTO "audit_log" VALUES(2,1.7912616437995553e+09,'COUNTER-2','price_update','{"product_id": "Z1", "new_price": 13.5}');
INSERT INTO "audit_log" VALUES(3,1.79126164404045438e+09,'COUNTER-2','sync_conflicts','1 conflict(s) resolved');
INSERT INTO "audit_log" VALUES(4,1.79126164409742927e+09,'COUNTER-2','sync','txns=1 deltas=2 updates=1 retries=0');
CREATE TABLE conflicts(
    conflict_id TEXT PRIMARY KEY,
    entity      TEXT NOT NULL,          -- 'product'
    entity_id   TEXT NOT NULL,
    local_value TEXT,
    remote_value TEXT,
    resolution  TEXT NOT NULL,          -- e.g. 'last-write-wins'
    resolved_at REAL NOT NULL
);
INSERT INTO "conflicts" VALUES('3e934a868100478b9b7ddb127296f1bf','product','Z1','{"product_id": "Z1", "name": "Gizmo", "price": 12.75, "stock": 40, "version": 1, "updated_at": 1791261643.6735084, "updated_by": "seed", "barcode": "8901011000060"}','{"update_id": "a960b689b410400b910cf4682202c213", "product_id": "Z1", "name": "Gizmo", "price": 13.5, "updated_at": 1791261643.7992508, "updated_by": "COUNTER-2", "synced": 0}','last-write-wins (incoming newer)',1.79126164402191972e+09);
CREATE TABLE kv(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO "kv" VALUES('last_sync_ts','1791261644.0784814');
CREATE TABLE product_updates(
    update_id  TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    price      REAL NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "product_updates" VALUES('a960b689b410400b910cf4682202c213','Z1','Gizmo',13.5,1.79126164379925084e+09,'COUNTER-2',1);
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
INSERT INTO "products" VALUES('B1','Bolt',2.0,26,1,1.79126164363328957e+09,'seed','8901011000046');
INSERT INTO "products" VALUES('N1','Nut',1.0,40,1,1.79126164365422868e+09,'seed','8901011000053');
INSERT INTO "products" VALUES('Z1','Gizmo',13.5,40,2,1.79126164379925084e+09,'COUNTER-2','8901011000060');
CREATE TABLE stock_deltas(
    delta_id    TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    delta       INTEGER NOT NULL,        -- negative = sale, positive = restock
    txn_id      TEXT,
    created_at  REAL NOT NULL,
    terminal_id TEXT NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "stock_deltas" VALUES('c88af292e6194d71b493aadafbb3ef3e','B1',-4,'fadf00d7e26e4a358cc2b930e65bc42f',1.79126164377877736e+09,'COUNTER-2',1);
INSERT INTO "stock_deltas" VALUES('bef1c24faa2949e7a13fa783d5e6e54e','N1',-10,'fadf00d7e26e4a358cc2b930e65bc42f',1.79126164377877736e+09,'COUNTER-2',1);
CREATE TABLE transactions(
    txn_id          TEXT PRIMARY KEY,
    terminal_id     TEXT NOT NULL,
    items_json      TEXT NOT NULL,      -- [[product_id, qty, unit_price], ...]
    total           REAL NOT NULL,
    created_at      REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    synced          INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "transactions" VALUES('fadf00d7e26e4a358cc2b930e65bc42f','COUNTER-2','[["B1", 4, 2.0], ["N1", 10, 1.0]]',18.0,1.79126164377877736e+09,'COUNTER-2:fadf00d7e26e4a358cc2b930e65bc42f',1);
CREATE UNIQUE INDEX idx_products_barcode ON products(barcode);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('audit_log',4);
COMMIT;
