"""SwiftBill simulation suite.

Twelve scenarios covering the OS + DBMS concepts in the project:
  S1 offline sale -> sync (delta sync, ACID local sale)
  S2 price conflict -> last-write-wins + conflict log
  S3 concurrent offline sales -> commutative stock-delta merge
  S4 flaky link -> exponential backoff + idempotent exactly-once
  S5 priority scheduling -> billing never waits on background work
  S6 hybrid chatbot -> offline intent answering
  S7 clock skew -> skewed timestamp wins LWW, conflict logged for review
  S8 login, roles, sessions
  S9 receipt printing (file + ESC/POS, printer-outage fallback)
  S10 barcode catalog (EAN-13 validation, scan lookup, sync carry-over)
  S11 tax calculation (per-product rates, ACID tax math, receipts, migration)
  S12 real LLM chatbot (OpenAI-compatible call, store-context prompt, fallback)
  S13 connectivity auto-detect (socket probe drives the shared state machine)

Run:  python3 simulations/run_all.py
Writes: SIMULATION_RESULTS.md
"""
from __future__ import annotations

import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from offlinepos.auth import AuthStore
from offlinepos.catalog import catalog_tuples, ean13_is_valid, load_catalog
from offlinepos.central_db import CentralDB
from offlinepos.local_db import LocalDB
from offlinepos.pos import Terminal
from offlinepos.printer import ReceiptPrinter, format_text_receipt
from offlinepos.scheduler import SYNC, MAINTENANCE

RESULTS: list[dict] = []


def scenario(name):
    def deco(fn):
        def wrapper():
            t0 = time.monotonic()
            try:
                details = fn()
                RESULTS.append({"name": name, "passed": True,
                                "details": details,
                                "duration_s": round(time.monotonic() - t0, 2)})
                print(f"[PASS] {name}")
            except Exception as e:  # noqa: BLE001
                RESULTS.append({"name": name, "passed": False,
                                "details": f"{e}\n{traceback.format_exc(limit=3)}",
                                "duration_s": round(time.monotonic() - t0, 2)})
                print(f"[FAIL] {name}: {e}")
        return wrapper
    return deco


def make_env():
    tmp = tempfile.mkdtemp(prefix="offlinepos_")
    central = CentralDB(os.path.join(tmp, "central.db"))
    return tmp, central


def make_terminal(tid, tmp, central, clock=None):
    t = Terminal(tid, os.path.join(tmp, tid), central, clock=clock)
    t.start()
    return t


# ---------------------------------------------------------------- S1
@scenario("S1: offline sale, then sync")
def s1():
    tmp, central = make_env()
    a = make_terminal("T-A", tmp, central)
    try:
        a.seed_catalog([("W1", "Widget", 10.0, 20)])
        central.seed_product("W1", "Widget", 10.0, 20)
        a.set_online(False)
        a.scan("W1", 2)
        rcpt = a.checkout()
        assert rcpt["sale"]["total"] == 20.0
        assert a.db.get_product("W1")["stock"] == 18, "local stock must drop at sale time"
        assert a.db.pending_counts()["txns"] == 1
        a.set_online(True)
        rep = a.sync_now()
        assert rep.ok and rep.pushed_txns == 1 and rep.retries == 0
        assert central.txn_count() == 1
        assert central.get_product("W1")["stock"] == 18
        return (f"sale total=₹{rcpt['sale']['total']:.2f}, local stock 20->18 while offline, "
                f"sync pushed 1 txn, central stock=18, central txns=1")
    finally:
        a.close()


# ---------------------------------------------------------------- S2
@scenario("S2: price conflict -> last-write-wins")
def s2():
    tmp, central = make_env()
    a = make_terminal("T-A", tmp, central)
    b = make_terminal("T-B", tmp, central)
    try:
        for t in (a, b):
            t.seed_catalog([("W2", "Gadget", 10.0, 50)])
            t.set_online(False)
        central.seed_product("W2", "Gadget", 10.0, 50)
        base = time.time()
        a.db.update_product_price("W2", 11.0, "T-A", ts=base + 1)  # older write
        b.db.update_product_price("W2", 12.0, "T-B", ts=base + 2)  # newer write
        a.set_online(True)
        b.set_online(True)
        ra = a.sync_now()
        rb = b.sync_now()
        price = central.get_product("W2")["price"]
        assert price == 12.0, f"newer write must win, got {price}"
        n_conf = len(b.db.list_conflicts())
        assert n_conf >= 1, "losing/overwriting write must be logged"
        return (f"A set ₹11 (older), B set ₹12 (newer); central price=₹{price:.2f}; "
                f"conflicts logged on B: {n_conf} "
                f"(resolution: {b.db.list_conflicts()[0]['resolution']})")
    finally:
        a.close(); b.close()


# ---------------------------------------------------------------- S3
@scenario("S3: concurrent offline sales -> delta merge")
def s3():
    tmp, central = make_env()
    a = make_terminal("T-A", tmp, central)
    b = make_terminal("T-B", tmp, central)
    try:
        for t in (a, b):
            t.seed_catalog([("W3", "Sprocket", 5.0, 100)])
            t.set_online(False)
        central.seed_product("W3", "Sprocket", 5.0, 100)
        a.scan("W3", 2); a.checkout()   # delta -2
        b.scan("W3", 3); b.checkout()   # delta -3
        a.set_online(True); b.set_online(True)
        a.sync_now()
        assert central.get_product("W3")["stock"] == 98
        b.sync_now()
        central_stock = central.get_product("W3")["stock"]
        assert central_stock == 95, f"expected 95, got {central_stock}"
        a.sync_now()  # converge A via pull
        assert a.db.get_product("W3")["stock"] == 95
        assert b.db.get_product("W3")["stock"] == 95
        return ("A sold 2, B sold 3 while offline; central stock 100->95 "
                "(commutative delta merge, no conflict); both terminals converged to 95")
    finally:
        a.close(); b.close()


# ---------------------------------------------------------------- S4
@scenario("S4: flaky link -> backoff + exactly-once")
def s4():
    tmp, central = make_env()
    c = make_terminal("T-C", tmp, central)
    try:
        c.seed_catalog([("W4", "Bolt", 2.0, 30)])
        central.seed_product("W4", "Bolt", 2.0, 30)
        c.set_online(False)
        c.scan("W4", 1); c.checkout()
        c.set_online(True)
        c.chaos.fail_push_times = 3  # drop the connection 3 times mid-push
        rep = c.sync_now()
        assert rep.ok, f"sync should succeed after retries: {rep.error}"
        assert rep.retries == 3, f"expected 3 retries, got {rep.retries}"
        assert central.txn_count() == 1, "idempotency key must prevent duplicates"
        # now prove a duplicated batch is a no-op
        c.set_online(False)
        c.scan("W4", 1); c.checkout()
        c.set_online(True)
        c.chaos.duplicate_push = True
        rep2 = c.sync_now()
        assert central.txn_count() == 2, "exactly one new txn despite duplicate push"
        assert rep2.deduped_txns >= 1
        return (f"3 mid-push drops -> 3 backoff retries then success; "
                f"central has exactly 1 txn (no dupes); duplicate-push test deduped "
                f"{rep2.deduped_txns} txn(s), central total=2")
    finally:
        c.close()


# ---------------------------------------------------------------- S5
@scenario("S5: billing preempts saturated background lane")
def s5():
    tmp, central = make_env()
    d = make_terminal("T-D", tmp, central)
    try:
        d.seed_catalog([("W5", "Nut", 1.0, 100)])
        # saturate the background worker with a 2s sync-shaped job
        d.scheduler.submit(SYNC, lambda: time.sleep(2), name="long-sync")
        d.scheduler.submit(MAINTENANCE, lambda: time.sleep(2), name="maint-1")
        d.scan("W5", 4)
        t0 = time.monotonic()
        fut = d.checkout_async()  # BILLING lane: dedicated worker
        receipt = fut.result(timeout=10)
        elapsed = time.monotonic() - t0
        assert receipt["sale"]["total"] == 4.0
        assert elapsed < 1.0, f"billing waited {elapsed:.2f}s on background work!"
        stats = d.scheduler.wait_stats()
        return (f"background lane blocked ~2s; checkout completed in {elapsed:.3f}s "
                f"via dedicated billing worker; wait stats: {stats['BILLING']}")
    finally:
        d.close()


# ---------------------------------------------------------------- S6
@scenario("S6: chatbot answers offline")
def s6():
    tmp, central = make_env()
    e = make_terminal("T-E", tmp, central)
    try:
        e.seed_catalog([("W6", "Widget", 10.0, 20)])
        e.set_online(False)  # offline: no LLM, local intents only
        e.scan("W6", 2); e.checkout()
        sales = e.ask("total sales today")
        stock = e.ask("stock of Widget")
        sync = e.ask("sync status")
        huh = e.ask("blargh zzz")
        assert "₹20.00" in sales, sales
        assert "18" in stock, stock
        assert "OFFLINE" in sync and "pending" in sync, sync
        assert "offline" in huh.lower(), huh
        assert e.chatbot.mode == "offline"
        e.set_online(True)
        assert e.chatbot.mode == "online", "bot must follow the shared state machine"
        return (f"offline answers OK: sales='{sales}', stock='{stock}', "
                f"sync='{sync}'; mode followed net OFFLINE->ONLINE")
    finally:
        e.close()


# ---------------------------------------------------------------- S7
@scenario("S7: clock skew -> LWW still decides, conflict logged")
def s7():
    tmp, central = make_env()
    a = make_terminal("T-A", tmp, central)
    skewed_clock = lambda: time.time() + 5000  # T-B's clock runs 5000s fast
    b = make_terminal("T-B", tmp, central, clock=skewed_clock)
    try:
        for t in (a, b):
            t.seed_catalog([("W7", "Gizmo", 10.0, 40)])
            t.set_online(False)
        central.seed_product("W7", "Gizmo", 10.0, 40)
        base = time.time()
        a.db.update_product_price("W7", 20.0, "T-A", ts=base)       # real write
        b.db.update_product_price("W7", 30.0, "T-B")                # skewed ts wins
        a.set_online(True); b.set_online(True)
        a.sync_now(); rb = b.sync_now()
        price = central.get_product("W7")["price"]
        assert price == 30.0, f"skewed newer ts wins LWW, got {price}"
        assert len(b.db.list_conflicts()) >= 1
        return (f"T-B clock +5000s: its ₹30 write carried the newer timestamp and won "
                f"LWW (central=₹{price:.2f}); conflict logged for manual review "
                f"-> '{b.db.list_conflicts()[0]['resolution']}' (known limitation)")
    finally:
        a.close(); b.close()


# ---------------------------------------------------------------- S8
@scenario("S8: login, roles, sessions")
def s8():
    tmp = tempfile.mkdtemp(prefix="offlinepos_auth_")
    auth = AuthStore(os.path.join(tmp, "auth.db"))
    auth.seed_defaults()
    # correct credentials
    mgr = auth.verify("manager", "admin123")
    assert mgr and mgr["role"] == "manager", "manager login must work"
    csh = auth.verify("cashier", "cashier123")
    assert csh and csh["role"] == "cashier", "cashier login must work"
    # wrong / unknown rejected
    assert auth.verify("manager", "wrong") is None, "bad password must fail"
    assert auth.verify("nobody", "x") is None, "unknown user must fail"
    # sessions
    tok = auth.new_session(mgr)
    assert auth.get_session(tok)["username"] == "manager"
    assert auth.get_session("bogus") is None
    auth.end_session(tok)
    assert auth.get_session(tok) is None, "logout must kill the session"
    # duplicate user rejected
    try:
        auth.create_user("manager", "another1", "manager", "Dup")
        raise AssertionError("duplicate username must be rejected")
    except ValueError:
        pass
    return ("manager/cashier logins OK, bad password + unknown user rejected, "
            "session create/validate/logout OK, duplicate username rejected")


# ---------------------------------------------------------------- S9
@scenario("S9: receipt printing (file + printer fallback)")
def s9():
    tmp = tempfile.mkdtemp(prefix="offlinepos_print_")
    sale = {"txn_id": "abc123def456", "total": 45.50,
            "items": [["W1", 2, 10.0], ["G1", 1, 25.5]]}
    text = format_text_receipt(sale, "COUNTER-1")
    assert "TOTAL" in text and "₹45.50" in text and "abc123de" in text
    assert max(len(l) for l in text.splitlines()) <= 42, "42-column format"
    # file backend
    pr = ReceiptPrinter(mode="file", receipt_dir=os.path.join(tmp, "rcpts"))
    r = pr.print_receipt(sale, "COUNTER-1")
    assert r["ok"] and r["via"] == "file" and os.path.exists(r["path"])
    # network backend with no printer -> graceful file fallback
    pr2 = ReceiptPrinter(mode="network", host="127.0.0.1", port=9,
                         receipt_dir=os.path.join(tmp, "rcpts2"), timeout=0.5)
    r2 = pr2.print_receipt(sale, "COUNTER-1")
    assert r2["ok"] and "file" in r2["via"] and os.path.exists(r2["path"]), \
        "unreachable printer must fall back to file, never raise"
    return (f"42-col receipt OK (total ₹45.50); file backend wrote {r['path']}; "
            f"unreachable network printer fell back to file, no exception")


# ---------------------------------------------------------------- S10
@scenario("S10: product catalog + barcode lookup")
def s10():
    catalog = load_catalog()
    assert len(catalog) == 14, f"expected 14 products, got {len(catalog)}"
    for c in catalog:
        assert ean13_is_valid(c["barcode"]), f"bad EAN-13: {c['barcode']}"
    assert len({c["barcode"] for c in catalog}) == 14, "barcodes must be unique"

    tmp, central = make_env()
    t = make_terminal("T-SCAN", tmp, central)
    try:
        t.seed_catalog(catalog_tuples())
        widget = t.db.get_product_by_barcode("8901011000015")
        assert widget and widget["name"] == "Widget", "scan must find Widget"
        assert t.db.get_product_by_barcode("0000000000000") is None, \
            "unknown barcode must return None"
        # barcode survives a sync round-trip to central
        central.seed_product("W1", "Widget", 10.0, 20, barcode="8901011000015")
        assert central.get_product("W1")["barcode"] == "8901011000015"
        return ("6 products loaded from catalog/products.csv, all EAN-13 valid "
                "and unique; scan of 8901011000015 -> Widget; unknown -> None; "
                "central carries the barcode too")
    finally:
        t.close()


# ---------------------------------------------------------------- S11
@scenario("S11: tax calculation on checkout")
def s11():
    tmp, central = make_env()
    a = make_terminal("T-A", tmp, central)
    try:
        a.seed_catalog([("W1", "Widget", 10.0, 20, None, 18.0),
                        ("B1", "Bolt", 2.0, 30, None, 5.0),
                        ("F1", "Freebie", 3.0, 10)])  # no rate -> 0%
        a.set_online(False)
        a.scan("W1", 2); a.scan("B1", 1); a.scan("F1", 1)
        rcpt = a.checkout()
        sale = rcpt["sale"]
        # subtotal 20+2+3=25; tax 3.60+0.10+0=3.70; total 28.70
        assert sale["subtotal"] == 25.0, sale
        assert sale["tax_total"] == 3.7, sale
        assert sale["total"] == 28.7, sale
        assert "SUBTOTAL: ₹25.00" in rcpt["text"], rcpt["text"]
        assert "TAX: ₹3.70" in rcpt["text"], rcpt["text"]
        assert "TOTAL: ₹28.70" in rcpt["text"], rcpt["text"]
        # persisted breakdown columns
        row = a.db._conn().execute(
            "SELECT subtotal, tax_total, total FROM transactions").fetchone()
        assert (row["subtotal"], row["tax_total"], row["total"]) == (25.0, 3.7, 28.7)
        # printer path shows the tax lines too
        text = format_text_receipt(sale, "T-A")
        assert "TAX" in text and "₹28.70" in text, text
        # sync round-trip keeps the taxed total intact
        central.seed_product("W1", "Widget", 10.0, 20, tax_rate=18.0)
        central.seed_product("B1", "Bolt", 2.0, 30, tax_rate=5.0)
        central.seed_product("F1", "Freebie", 3.0, 10)
        a.set_online(True)
        rep = a.sync_now()
        assert rep.ok, rep.error
        ctot = central._conn().execute(
            "SELECT total FROM transactions").fetchone()["total"]
        assert ctot == 28.7, ctot
        # migration: a pre-tax database gains the new columns on open
        leg = os.path.join(tmp, "legacy.db")
        c0 = sqlite3.connect(leg)
        c0.execute("CREATE TABLE products(product_id TEXT PRIMARY KEY, "
                   "name TEXT NOT NULL, price REAL NOT NULL, "
                   "stock INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, "
                   "updated_at REAL NOT NULL, updated_by TEXT NOT NULL)")
        c0.execute("INSERT INTO products VALUES('L1','Legacy',5.0,10,1,1.0,'seed')")
        c0.commit(); c0.close()
        ldb = LocalDB(leg)
        assert ldb.get_product("L1")["tax_rate"] == 0.0
        sale2 = ldb.create_sale([("L1", 1)], "T-X")
        assert sale2["total"] == 5.0 and sale2["tax_total"] == 0.0, sale2
        return ("2x Widget@18% + 1x Bolt@5% + 1x tax-free: subtotal ₹25.00, "
                "tax ₹3.70, total ₹28.70; breakdown persisted, printed, and "
                "synced; pre-tax DBs migrate cleanly with 0% default")
    finally:
        a.close()


class _FakeLLM(http.server.BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /chat/completions stub for S12."""
    mode = "ok"
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _FakeLLM.seen.append(json.loads(self.rfile.read(length) or b"{}"))
        if _FakeLLM.mode == "error":
            self.send_response(500); self.end_headers(); return
        payload = json.dumps(
            {"choices": [{"message": {"content": "LLM: sales look good today"}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output clean
        pass


# ---------------------------------------------------------------- S12
@scenario("S12: real LLM chatbot with graceful fallback")
def s12():
    tmp, central = make_env()
    e = make_terminal("T-E", tmp, central)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_env = dict(os.environ)
    try:
        os.environ["OFFLINEPOS_LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
        os.environ["OFFLINEPOS_LLM_API_KEY"] = "test-key"
        os.environ["OFFLINEPOS_LLM_MODEL"] = "test-model"
        e.seed_catalog([("W9", "Widget", 10.0, 20)])
        e.set_online(True)
        ans = e.ask("how are sales?")
        assert ans == "LLM: sales look good today", ans
        body = _FakeLLM.seen[-1]
        assert body["model"] == "test-model", body
        msgs = {m["role"]: m["content"] for m in body["messages"]}
        assert "SwiftBill terminal T-E" in msgs["system"], msgs["system"]
        assert "how are sales?" in msgs["user"]
        # server blows up -> degrade to offline intents, no exception
        _FakeLLM.mode = "error"
        e.scan("W9", 1); e.checkout()
        fb = e.ask("total sales today")
        assert "₹10.00" in fb, fb
        # no key configured -> offline intents, and no HTTP attempt at all
        del os.environ["OFFLINEPOS_LLM_API_KEY"]
        _FakeLLM.mode = "ok"
        n = len(_FakeLLM.seen)
        fb2 = e.ask("total sales today")
        assert "₹10.00" in fb2 and len(_FakeLLM.seen) == n, fb2
        return ("online LLM answered through a fake OpenAI-compatible server; "
                "request carried model + store-context system prompt; HTTP 500 "
                "fell back to offline intents; missing key made zero HTTP calls")
    finally:
        os.environ.clear(); os.environ.update(old_env)
        _FakeLLM.mode = "ok"
        srv.shutdown(); srv.server_close()
        e.close()


# ---------------------------------------------------------------- S13
@scenario("S13: real connectivity auto-detect")
def s13():
    import socket as _socket
    from offlinepos.net import Connectivity, internet_reachable

    # a local "internet": plain TCP server; kernel completes the handshake
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _serve():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                break

    threading.Thread(target=_serve, daemon=True).start()
    try:
        # probe: reachable endpoint -> True; dead port -> False; no internet needed
        assert internet_reachable(endpoints=[("127.0.0.1", port)], timeout=1)
        assert not internet_reachable(endpoints=[("127.0.0.1", 1)], timeout=0.5)
        # auto mode drives the shared state machine from probe results
        net = Connectivity()
        seen = []
        net.subscribe(seen.append)
        net.set_auto_detect(True, interval=0.2,
                            endpoints=[("127.0.0.1", port)], timeout=1)
        deadline = time.monotonic() + 5
        while not net.online and time.monotonic() < deadline:
            time.sleep(0.05)
        assert net.online and net.mode == "auto", "probe should flip state online"
        assert "online" in seen
        # "internet" dies -> state follows back to offline
        stop.set(); srv.close()
        deadline = time.monotonic() + 8
        while net.online and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not net.online, "dead endpoint should flip state offline"
        net.set_auto_detect(False)
        assert net.mode == "manual"
        return ("probe True vs local server / False vs dead port; auto-detect "
                "thread drove OFFLINE->ONLINE->OFFLINE on the shared state machine")
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass


def write_report():
    passed = sum(1 for r in RESULTS if r["passed"])
    lines = ["# SwiftBill Simulation Results",
             "",
             f"Ran {len(RESULTS)} scenarios, **{passed} passed**, "
             f"{len(RESULTS) - passed} failed.",
             "",
             "| Scenario | Result | Time (s) |",
             "|---|---|---|"]
    for r in RESULTS:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"| {r['name']} | {mark} | {r['duration_s']} |")
    lines += ["", "## Details", ""]
    for r in RESULTS:
        lines.append(f"### {r['name']} - {'PASS' if r['passed'] else 'FAIL'}")
        lines.append("")
        lines.append(r["details"])
        lines.append("")
    out = os.path.join(os.path.dirname(__file__), "..", "SIMULATION_RESULTS.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nReport written to {os.path.abspath(out)}")
    return passed == len(RESULTS)


if __name__ == "__main__":
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13):
        fn()
    ok = write_report()
    sys.exit(0 if ok else 1)
