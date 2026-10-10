"""SwiftBill web backend.

FastAPI over the existing engine — the browser never touches SQLite
directly. Runs on macOS / Windows / Linux: any machine with Python 3.10+
and a browser.

Run:
    uvicorn web.backend.app:app --host 127.0.0.1 --port 8000
Then open http://localhost:8000

The server is meant to run ON the terminal (localhost), so the POS keeps
working with no internet — exactly the offline-first story. The central
DB sync path is unchanged.
"""
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from offlinepos.auth import AuthStore
from offlinepos.catalog import load_catalog
from offlinepos.central_db import CentralDB
from offlinepos.pos import Terminal
from offlinepos.printer import ReceiptPrinter

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("OFFLINEPOS_DATA", ROOT / "data"))
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


def seed_if_empty():
    assert central is not None
    for tid in TERMINAL_IDS:
        t = terminals[tid]
        if not t.db.list_products():
            t.seed_catalog(list(CATALOG))
    if central.get_product(CATALOG[0][0]) is None:
        for pid, name, price, stock, barcode, tax_rate in CATALOG:
            central.seed_product(pid, name, price, stock, barcode=barcode,
                                 tax_rate=tax_rate)


def build_world():
    """(Re)create central + terminals. Used at startup and by /demo/reset."""
    global central, auth_store, printer
    for t in terminals.values():
        try:
            t.close()
        except Exception:
            pass
    terminals.clear()
    DATA.mkdir(parents=True, exist_ok=True)
    for f in DATA.rglob("*.db*"):
        try:
            f.unlink()
        except OSError:
            pass
    central = CentralDB(DATA / "central.db")
    auth_store = AuthStore(DATA / "auth.db")
    auth_store.seed_defaults()
    printer = ReceiptPrinter(receipt_dir=DATA / "receipts")
    for tid in TERMINAL_IDS:
        t = Terminal(tid, DATA / tid, central)
        t.start()
        terminals[tid] = t
    if os.environ.get("OFFLINEPOS_NET_MODE", "manual").lower() == "auto":
        for t in terminals.values():
            t.set_net_mode("auto")
    seed_if_empty()


@asynccontextmanager
async def lifespan(app: FastAPI):
    build_world()
    yield
    for t in terminals.values():
        try:
            t.close()
        except Exception:
            pass


app = FastAPI(title="SwiftBill", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- models
class ItemIn(BaseModel):
    product_id: str
    qty: int = Field(gt=0)


class CheckoutIn(BaseModel):
    items: list[ItemIn]


class NetIn(BaseModel):
    online: bool


class NetModeIn(BaseModel):
    mode: str = Field(pattern="^(auto|manual)$")


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class PriceIn(BaseModel):
    product_id: str
    price: float = Field(gt=0)


class ReviewIn(BaseModel):
    conflict_id: str = Field(min_length=1)


class LoginIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


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
def api_receipt_download(terminal: str = Query("COUNTER-1"),
                         txn_id: str = Query(...),
                         user: dict = Depends(current_user)):
    """Download a previously printed receipt as a .txt file.

    Works on hosting without disk/shell access (e.g. Render free tier):
    the file is streamed to the browser instead of read off the server.
    """
    import re
    get_terminal(terminal)  # validate terminal id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", txn_id):
        raise HTTPException(400, "bad txn_id")
    assert printer is not None
    fname = f"receipt_{txn_id[:8]}_{terminal}.txt"
    path = Path(printer.receipt_dir) / fname
    if not path.is_file():
        raise HTTPException(404, "receipt not found — print it first")
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=fname)


# ---------------------------------------------------------------- sync
@app.get("/api/sync/status")
def api_sync_status(terminal: str = Query("COUNTER-1"),
                    manager: dict = Depends(manager_only)):
    t = get_terminal(terminal)
    return {"terminal": terminal,
            "online": t.net.online,
            "pending": t.db.pending_counts(),
            "last_sync_ts": t.db.get_kv("last_sync_ts")}


@app.post("/api/sync/now")
def api_sync_now(terminal: str = Query("COUNTER-1"),
                 manager: dict = Depends(manager_only)):
    rep = get_terminal(terminal).sync_now()
    return {"ok": rep.ok, "pushed_txns": rep.pushed_txns,
            "pushed_deltas": rep.pushed_deltas,
            "pushed_updates": rep.pushed_updates,
            "deduped_txns": rep.deduped_txns,
            "deduped_deltas": rep.deduped_deltas,
            "conflicts": rep.conflicts, "retries": rep.retries,
            "error": rep.error, "duration_s": round(rep.duration_s, 3)}


@app.post("/api/net")
def api_net(body: NetIn):
    """Manual (simulated) connectivity switch — the demo/chaos control."""
    for t in terminals.values():
        t.set_online(body.online)
    return {"online": body.online}


@app.post("/api/net/mode")
def api_net_mode(body: NetModeIn, user: dict = Depends(current_user)):
    """'manual' (UI toggle drives state) or 'auto' (real probe drives it)."""
    modes = {t.set_net_mode(body.mode) for t in terminals.values()}
    return {"mode": body.mode if len(modes) == 1 else "mixed"}


@app.get("/api/net/status")
def api_net_status(user: dict = Depends(current_user)):
    t = terminals["COUNTER-1"]
    return {"online": t.net.online, "mode": t.net.mode}


@app.get("/api/conflicts")
def api_conflicts(terminal: str = Query("COUNTER-1"),
                  manager: dict = Depends(manager_only)):
    return {"conflicts": get_terminal(terminal).db.list_conflicts()}


@app.post("/api/conflicts/review")
def api_conflicts_review(body: ReviewIn, terminal: str = Query("COUNTER-1"),
                         manager: dict = Depends(manager_only)):
    """Manager marks a conflict as reviewed (ADR-001 loser stays logged)."""
    ok = get_terminal(terminal).db.mark_conflict_reviewed(
        body.conflict_id.strip())
    if not ok:
        raise HTTPException(404, "conflict not found")
    return {"ok": True}


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
    return {"answer": t.ask(body.question)}


# ---------------------------------------------------------------- demos
@app.post("/api/demo/conflict")
def api_demo_conflict(manager: dict = Depends(manager_only)):
    """Run the two-terminal price-conflict story and report the outcome."""
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
    assert central is not None
    return {"central_price": central.get_product("G1")["price"],
            "conflicts_on_counter_2": len(b.db.list_conflicts()),
            "note": "newer write won last-write-wins; loser logged"}


@app.post("/api/demo/reset")
def api_demo_reset(manager: dict = Depends(manager_only)):
    build_world()
    return {"ok": True}


# ---------------------------------------------------------------- frontend
FRONTEND = ROOT / "web" / "frontend"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
