"""OfflinePOS web backend.

FastAPI over the existing engine — the browser never touches SQLite
directly. Runs on macOS / Windows / Linux: any machine with Python 3.10+
and a browser.

The central database is MySQL (or MariaDB). Configure it with:
    OFFLINEPOS_MYSQL_HOST      (default 127.0.0.1)
    OFFLINEPOS_MYSQL_PORT      (default 3306)
    OFFLINEPOS_MYSQL_USER      (default offlinepos)
    OFFLINEPOS_MYSQL_PASSWORD
    OFFLINEPOS_MYSQL_DATABASE  (default offlinepos_central)
Other options:
    OFFLINEPOS_DATA            local data dir (terminal DBs, auth, receipts)
    OFFLINEPOS_WIPE_ON_START   set to 1 to reset everything at startup
                               (local DBs AND the MySQL tables) - demo only

Run:
    uvicorn web.backend.app:app --host 127.0.0.1 --port 8000
Then open http://localhost:8000

The server is meant to run ON the terminal (localhost), so the POS keeps
working with no internet — exactly the offline-first story. If MySQL is
unreachable the terminal still boots and bills; sync simply reports an error
and retries once the central server is back.
"""
import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from offlinepos.auth import AuthStore
from offlinepos.catalog import load_catalog
from offlinepos.central_db import CentralDB
from offlinepos.cluster import sync_all
from offlinepos.pos import Terminal
from offlinepos.printer import ReceiptPrinter, format_text_receipt

log = logging.getLogger("offlinepos")

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("OFFLINEPOS_DATA", ROOT / "data"))
WIPE_ON_START = os.environ.get("OFFLINEPOS_WIPE_ON_START", "0") == "1"
TERMINAL_IDS = ("COUNTER-1", "COUNTER-2")
# Source of truth: catalog/products.csv (id, EAN-13 barcode, name, price, stock, tax_rate)
CATALOG = [(c["product_id"], c["name"], c["price"], c["stock"], c["barcode"],
            c["tax_rate"])
           for c in load_catalog()]

central: CentralDB | None = None
terminals: dict[str, Terminal] = {}
auth_store: AuthStore | None = None
printer: ReceiptPrinter | None = None
security = HTTPBearer(auto_error=False)

_central_seeded = False
_seed_lock = threading.Lock()

# connectivity: "manual" = the ONLINE/OFFLINE pill is a simulation toggle,
# "auto" = a background probe follows real reachability of the central DB.
net_mode = "manual"
PROBE_INTERVAL_S = 5.0
_probe_stop = threading.Event()
_probe_thread: threading.Thread | None = None


def _probe_loop():
    while not _probe_stop.wait(PROBE_INTERVAL_S):
        if net_mode != "auto" or central is None:
            continue
        up = central.ping()
        for t in list(terminals.values()):
            t.set_online(up)


def _start_probe():
    global _probe_thread
    if _probe_thread is None or not _probe_thread.is_alive():
        _probe_stop.clear()
        _probe_thread = threading.Thread(target=_probe_loop, daemon=True,
                                         name="net-probe")
        _probe_thread.start()


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    assert auth_store is not None
    user = auth_store.get_session(creds.credentials if creds else None)
    if user is None:
        raise HTTPException(401, "not signed in")
    return user


def manager_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "manager":
        raise HTTPException(403, "managers only")
    return user


def get_terminal(tid: str) -> Terminal:
    t = terminals.get(tid)
    if t is None:
        raise HTTPException(400, f"unknown terminal {tid!r}")
    return t


def ensure_central_seeded() -> bool:
    """Seed the MySQL catalog once. Returns False if MySQL is unreachable.

    Safe to call repeatedly; it is retried on the next call until it succeeds,
    so a terminal that booted while the central server was down still gets
    its products registered centrally before the first sync.
    """
    global _central_seeded
    assert central is not None
    if _central_seeded:
        return True
    with _seed_lock:
        if _central_seeded:
            return True
        try:
            if central.get_product(CATALOG[0][0]) is None:
                for pid, name, price, stock, barcode, tax_rate in CATALOG:
                    central.seed_product(pid, name, price, stock,
                                         barcode=barcode, tax_rate=tax_rate)
        except ConnectionError as e:
            log.warning("central DB not reachable, will seed later: %s", e)
            return False
        _central_seeded = True
        return True


def seed_terminals():
    for tid in TERMINAL_IDS:
        t = terminals[tid]
        if not t.db.list_products():
            t.seed_catalog(list(CATALOG))


def build_world(wipe: bool = False):
    """(Re)create terminals, auth and the central connection.

    ``wipe=True`` deletes the local terminal/auth databases AND empties the
    MySQL tables (demo reset). Normal startup keeps all data, so unsynced
    sales survive a restart.
    """
    global central, auth_store, printer, _central_seeded
    for t in terminals.values():
        try:
            t.close()
        except Exception:
            pass
    terminals.clear()
    DATA.mkdir(parents=True, exist_ok=True)

    if central is None:
        central = CentralDB.from_env()  # lazy: does not connect yet
    if wipe:
        for f in DATA.rglob("*.db*"):
            try:
                f.unlink()
            except OSError:
                pass
        central.reset()  # raises ConnectionError if MySQL is down
    _central_seeded = False

    auth_store = AuthStore(DATA / "auth.db")
    auth_store.seed_defaults()
    printer = ReceiptPrinter(receipt_dir=DATA / "receipts")
    for tid in TERMINAL_IDS:
        t = Terminal(tid, DATA / tid, central)
        t.start()
        terminals[tid] = t
    seed_terminals()
    ensure_central_seeded()  # best effort; retried before each sync
    _start_probe()


@asynccontextmanager
async def lifespan(app: FastAPI):
    build_world(wipe=WIPE_ON_START)
    yield
    _probe_stop.set()
    for t in terminals.values():
        try:
            t.close()
        except Exception:
            pass


app = FastAPI(title="OfflinePOS", version="1.1.0", lifespan=lifespan)


# ---------------------------------------------------------------- models
class ItemIn(BaseModel):
    product_id: str
    qty: int = Field(gt=0)


class CheckoutIn(BaseModel):
    items: list[ItemIn]


class NetIn(BaseModel):
    online: bool


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class PriceIn(BaseModel):
    product_id: str
    price: float = Field(gt=0)


class LoginIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class NetModeIn(BaseModel):
    mode: str = Field(pattern="^(manual|auto)$")


class ReviewIn(BaseModel):
    conflict_id: str


class PrintIn(BaseModel):
    sale: dict
    terminal: str = "COUNTER-1"


# ---------------------------------------------------------------- auth
@app.post("/api/auth/login")
def api_login(body: LoginIn):
    assert auth_store is not None
    user = auth_store.verify(body.username.strip(), body.password)
    if user is None:
        raise HTTPException(401, "invalid username or password")
    token = auth_store.new_session(user)
    return {"token": token, "username": user["username"],
            "name": user["name"], "role": user["role"]}


@app.post("/api/auth/logout")
def api_logout(user: dict = Depends(current_user),
               creds: HTTPAuthorizationCredentials | None = Depends(security)):
    assert auth_store is not None
    auth_store.end_session(creds.credentials if creds else None)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(current_user)):
    return {"username": user["username"], "name": user["name"],
            "role": user["role"]}


# ---------------------------------------------------------------- shop
@app.get("/api/terminals")
def api_terminals(user: dict = Depends(current_user)):
    return {"terminals": list(terminals)}


@app.get("/api/products")
def api_products(terminal: str = Query("COUNTER-1"),
                 user: dict = Depends(current_user)):
    return {"products": get_terminal(terminal).db.list_products()}


@app.get("/api/products/by-barcode/{code}")
def api_by_barcode(code: str, terminal: str = Query("COUNTER-1"),
                   user: dict = Depends(current_user)):
    """Scanner lookup: barcode -> product (404 if unknown)."""
    prod = get_terminal(terminal).db.get_product_by_barcode(code)
    if prod is None:
        raise HTTPException(404, f"no product with barcode {code!r}")
    return prod


@app.post("/api/cart/checkout")
def api_checkout(body: CheckoutIn, terminal: str = Query("COUNTER-1"),
                 user: dict = Depends(current_user)):
    t = get_terminal(terminal)
    try:
        receipt = t.checkout_items([(i.product_id, i.qty) for i in body.items])
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return receipt


@app.post("/api/products/price")
def api_price(body: PriceIn, terminal: str = Query("COUNTER-1"),
              manager: dict = Depends(manager_only)):
    t = get_terminal(terminal)
    try:
        return t.db.update_product_price(body.product_id, body.price,
                                         t.terminal_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/receipt/print")
def api_print(body: PrintIn, user: dict = Depends(current_user)):
    assert printer is not None
    get_terminal(body.terminal)  # validate terminal id
    try:
        result = printer.print_receipt(body.sale, body.terminal)
    except (KeyError, TypeError) as e:
        raise HTTPException(400, f"bad receipt data: {e}")
    return result


@app.get("/api/receipt/download")
def api_receipt_download(txn_id: str, terminal: str = Query("COUNTER-1"),
                         user: dict = Depends(current_user)):
    """Re-create a sale's receipt from the local DB and send it as a file."""
    t = get_terminal(terminal)
    row = t.db._conn().execute("SELECT * FROM transactions WHERE txn_id=?",
                               (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no sale {txn_id!r} on {terminal}")
    r = dict(row)
    sale = {"txn_id": r["txn_id"], "items": json.loads(r["items_json"]),
            "subtotal": r.get("subtotal", r["total"]),
            "tax_total": r.get("tax_total", 0.0), "total": r["total"]}
    return Response(
        format_text_receipt(sale, terminal), media_type="text/plain",
        headers={"Content-Disposition":
                 f'attachment; filename="receipt_{txn_id[:8]}_{terminal}.txt"'})


# ---------------------------------------------------------------- sync
@app.get("/api/sync/status")
def api_sync_status(terminal: str = Query("COUNTER-1"),
                    manager: dict = Depends(manager_only)):
    t = get_terminal(terminal)
    return {"terminal": terminal,
            "online": t.net.online,
            "pending": t.db.pending_counts(),
            "last_sync_ts": t.db.get_kv("last_sync_ts")}


def _report_dict(rep) -> dict:
    return {"ok": rep.ok, "pushed_txns": rep.pushed_txns,
            "pushed_deltas": rep.pushed_deltas,
            "pushed_updates": rep.pushed_updates,
            "deduped_txns": rep.deduped_txns,
            "deduped_deltas": rep.deduped_deltas,
            "conflicts": rep.conflicts, "retries": rep.retries,
            "error": rep.error, "duration_s": round(rep.duration_s, 3)}


@app.post("/api/sync/now")
def api_sync_now(terminal: str = Query("COUNTER-1"),
                 manager: dict = Depends(manager_only)):
    get_terminal(terminal)  # validate the id
    ensure_central_seeded()  # no-op once seeded; failure shows up in rep.error
    # Sync the clicked counter, then every other counter, then converge all,
    # so one click updates the inventory on BOTH counters' local databases.
    reports = sync_all(terminals, terminal)
    per_terminal = {tid: _report_dict(r) for tid, r in reports.items()}
    return {**per_terminal[terminal],
            "ok": all(r.ok for r in reports.values()),
            "terminals": per_terminal,
            "synced_counters": list(reports)}


@app.get("/api/central/status")
def api_central_status(manager: dict = Depends(manager_only)):
    """Is the central MySQL server reachable right now?"""
    assert central is not None
    return {"reachable": central.ping(), "database": central.database}


@app.get("/api/net/status")
def api_net_status(terminal: str = Query("COUNTER-1"),
                   user: dict = Depends(current_user)):
    """Connectivity of the selected counter, the mode, and every counter."""
    t = get_terminal(terminal)
    return {"terminal": terminal,
            "mode": net_mode,
            "online": t.net.online,
            "state": t.net.state,
            "terminals": {tid: {"online": x.net.online, "state": x.net.state}
                          for tid, x in terminals.items()}}


@app.post("/api/net/mode")
def api_net_mode(body: NetModeIn, terminal: str = Query("COUNTER-1"),
                 user: dict = Depends(current_user)):
    global net_mode
    net_mode = body.mode
    online = get_terminal(terminal).net.online
    if net_mode == "auto":  # take effect immediately, not after the next probe
        assert central is not None
        online = central.ping()
        for t in terminals.values():
            t.set_online(online)
    return {"mode": net_mode, "online": online}


@app.post("/api/net")
def api_net(body: NetIn):
    for t in terminals.values():
        t.set_online(body.online)
    return {"online": body.online}


@app.get("/api/conflicts")
def api_conflicts(terminal: str = Query("COUNTER-1"),
                  manager: dict = Depends(manager_only)):
    rows = get_terminal(terminal).db.list_conflicts()
    for r in rows:
        # the page JSON.parse()s these, so always send JSON text
        for k in ("local_value", "remote_value"):
            if not isinstance(r.get(k), str):
                r[k] = json.dumps(r.get(k))
        r["reviewed"] = int(bool(r.get("reviewed", 0)))
    return {"conflicts": rows}


@app.post("/api/conflicts/review")
def api_conflict_review(body: ReviewIn, terminal: str = Query("COUNTER-1"),
                        manager: dict = Depends(manager_only)):
    """Toggle a conflict's reviewed flag (adds the column on first use)."""
    db = get_terminal(terminal).db
    try:
        with db.write_txn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(conflicts)")}
            if "reviewed" not in cols:
                c.execute("ALTER TABLE conflicts "
                          "ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
            cur = c.execute(
                "UPDATE conflicts SET reviewed = 1 - COALESCE(reviewed, 0) "
                "WHERE conflict_id=?", (body.conflict_id,))
            if cur.rowcount == 0:
                raise KeyError(body.conflict_id)
            row = c.execute("SELECT reviewed FROM conflicts WHERE conflict_id=?",
                            (body.conflict_id,)).fetchone()
    except KeyError:
        raise HTTPException(404, f"no conflict {body.conflict_id!r}")
    return {"conflict_id": body.conflict_id, "reviewed": int(row["reviewed"])}


@app.get("/api/health")
def api_health(terminal: str = Query("COUNTER-1"),
               user: dict = Depends(current_user)):
    t = get_terminal(terminal)
    return {"terminal": terminal, "online": t.net.online,
            "pending": t.db.pending_counts(),
            "last_sync_ts": t.db.get_kv("last_sync_ts"),
            "conflicts": len(t.db.list_conflicts()),
            "products": t.db.list_products()}


# ---------------------------------------------------------------- chat
@app.post("/api/chat/ask")
def api_chat(body: ChatIn, terminal: str = Query("COUNTER-1"),
             user: dict = Depends(current_user)):
    t = get_terminal(terminal)
    return {"answer": t.ask(body.question), "mode": t.chatbot.mode}


# ---------------------------------------------------------------- demos
@app.post("/api/demo/conflict")
def api_demo_conflict(manager: dict = Depends(manager_only)):
    """Run the two-terminal price-conflict story and report the outcome."""
    assert central is not None
    if not ensure_central_seeded():
        raise HTTPException(503, "central database is unreachable")
    a, b = terminals["COUNTER-1"], terminals["COUNTER-2"]
    for t in (a, b):
        t.set_online(False)
    base = time.time()
    a.db.update_product_price("G1", 27.0, "COUNTER-1", ts=base + 1)
    b.db.update_product_price("G1", 29.0, "COUNTER-2", ts=base + 2)
    for t in (a, b):
        t.set_online(True)
    a.sync_now()
    b.sync_now()
    prod = central.get_product("G1")
    return {"central_price": prod["price"] if prod else None,
            "conflicts_on_counter_2": len(b.db.list_conflicts()),
            "note": "newer write won last-write-wins; loser logged"}


@app.post("/api/demo/reset")
def api_demo_reset(manager: dict = Depends(manager_only)):
    try:
        build_world(wipe=True)
    except ConnectionError as e:
        raise HTTPException(503, f"central database is unreachable: {e}")
    return {"ok": True}


# ---------------------------------------------------------------- frontend
FRONTEND = ROOT / "web" / "frontend"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")