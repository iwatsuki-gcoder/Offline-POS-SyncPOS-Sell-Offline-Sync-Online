BEGIN TRANSACTION;
CREATE TABLE applied_deltas(
    delta_id TEXT PRIMARY KEY
);
INSERT INTO "applied_deltas" VALUES('6e31e8c5cb4f47feaa47ee7fcb4979d3');
INSERT INTO "applied_deltas" VALUES('9d831c9d6b004ca785935ac88f0db76f');
INSERT INTO "applied_deltas" VALUES('fed307b921f7477a82e46edb60d39684');
INSERT INTO "applied_deltas" VALUES('c88af292e6194d71b493aadafbb3ef3e');
INSERT INTO "applied_deltas" VALUES('bef1c24faa2949e7a13fa783d5e6e54e');
CREATE TABLE central_audit(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);
INSERT INTO "central_audit" VALUES(1,1.79126164382311725e+09,'apply_batch','txns=2/0 deltas=3/0');
INSERT INTO "central_audit" VALUES(2,1.79126164394128656e+09,'apply_batch','txns=1/0 deltas=2/0');
CREATE TABLE products(
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,
    updated_by  TEXT NOT NULL
, barcode TEXT);
INSERT INTO "products" VALUES('W1','Widget',10.0,18,1,1.79126164354405856e+09,'seed','8901011000015');
INSERT INTO "products" VALUES('G1','Gadget',25.5,14,1,1.7912616435924015e+09,'seed','8901011000022');
INSERT INTO "products" VALUES('S1','Sprocket',5.0,90,1,1.79126164361287879e+09,'seed','8901011000039');
INSERT INTO "products" VALUES('B1','Bolt',2.0,26,1,1.79126164363328957e+09,'seed','8901011000046');
INSERT INTO "products" VALUES('N1','Nut',1.0,40,1,1.79126164365422868e+09,'seed','8901011000053');
INSERT INTO "products" VALUES('Z1','Gizmo',13.5,40,2,1.79126164379925084e+09,'COUNTER-2','8901011000060');
CREATE TABLE transactions(
    txn_id          TEXT PRIMARY KEY,
    terminal_id     TEXT NOT NULL,
    items_json      TEXT NOT NULL,
    total           REAL NOT NULL,
    created_at      REAL NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);
INSERT INTO "transactions" VALUES('f8f9888c2b104f97b69ec4058df073bb','COUNTER-1','[["W1", 2, 10.0], ["G1", 1, 25.5]]',45.5,1.79126164369149518e+09,'COUNTER-1:f8f9888c2b104f97b69ec4058df073bb');
INSERT INTO "transactions" VALUES('c1c1d14f7a854a8082c2375e27229027','COUNTER-1','[["S1", 10, 5.0]]',50.0,1.79126164375877761e+09,'COUNTER-1:c1c1d14f7a854a8082c2375e27229027');
INSERT INTO "transactions" VALUES('fadf00d7e26e4a358cc2b930e65bc42f','COUNTER-2','[["B1", 4, 2.0], ["N1", 10, 1.0]]',18.0,1.79126164377877736e+09,'COUNTER-2:fadf00d7e26e4a358cc2b930e65bc42f');
CREATE UNIQUE INDEX idx_products_barcode ON products(barcode);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('central_audit',2);
COMMIT;
