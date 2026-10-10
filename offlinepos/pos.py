"""POS terminal: the billing hot path.

A sale is submitted to the scheduler at BILLING priority, so it preempts any
background sync/retry work. The actual DB write is one ACID transaction
(``LocalDB.create_sale``): stock check + decrement + txn record are
all-or-nothing, so a crash mid-checkout can never leave a half-written sale.
"""
from __future__ import annotations

import threading
from pathlib import Path

from .central_db import CentralDB
from .chaos import Chaos
from .chatbot import Chatbot
from .local_db import LocalDB
from .net import Connectivity
from .scheduler import BILLING, CHATBOT, SYNC, PriorityScheduler
from .sync_engine import SyncEngine


class Terminal:
    def __init__(self, terminal_id: str, workdir: str | Path,
                 central: CentralDB, clock=None):
        self.terminal_id = terminal_id
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        self.db = LocalDB(workdir / f"{terminal_id}.db", clock=clock)
        self.central = central
        self.net = Connectivity()
        self.scheduler = PriorityScheduler()
        self.chaos = Chaos()
        self.sync = SyncEngine(self.db, central, self.net, terminal_id,
                               chaos=self.chaos)
        self.chatbot = Chatbot(self.db, self.net, terminal_id)
        self._cart: list[tuple[str, int]] = []
        self._cart_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self.scheduler.start()

    def close(self):
        self.net.set_auto_detect(False)
        self.sync.stop_background()
        self.scheduler.stop()

    def set_online(self, value: bool):
        self.net.set_online(value)

    def set_net_mode(self, mode: str) -> str:
        """'manual' (UI toggle drives state) or 'auto' (real probe drives it)."""
        if mode not in ("manual", "auto"):
            raise ValueError(f"unknown net mode {mode!r}")
        return self.net.set_auto_detect(mode == "auto")

    # -- catalog --------------------------------------------------------------
    def seed_catalog(self, items: list[tuple]):
        """Items: (product_id, name, price, stock[, barcode[, tax_rate]])."""
        for item in items:
            pid, name, price, stock = item[:4]
            barcode = item[4] if len(item) > 4 else None
            tax_rate = item[5] if len(item) > 5 else 0.0
            self.db.seed_product(pid, name, price, stock, self.terminal_id,
                                 barcode=barcode, tax_rate=tax_rate)

    # -- billing ---------------------------------------------------------------
    def scan(self, product_id: str, qty: int = 1):
        with self._cart_lock:
            self._cart.append((product_id, qty))

    def clear_cart(self):
        with self._cart_lock:
            self._cart = []

    def checkout_async(self):
        """Checkout runs at BILLING priority; returns a Future of the receipt."""
        with self._cart_lock:
            items = list(self._cart)
            self._cart = []

        def _do():
            sale = self.db.create_sale(items, self.terminal_id)
            return self._receipt(sale)

        return self.scheduler.submit(BILLING, _do, name="checkout")

    def checkout(self, timeout=30) -> dict:
        return self.checkout_async().result(timeout=timeout)

    def checkout_items(self, items: list[tuple[str, int]], timeout=30) -> dict:
        """Checkout an explicit item list (used by the web API).

        Bypasses the interactive cart so concurrent API requests can't
        interleave. Still runs at BILLING priority through one ACID txn.
        """
        def _do():
            sale = self.db.create_sale(list(items), self.terminal_id)
            return self._receipt(sale)

        return self.scheduler.submit(BILLING, _do, name="checkout").result(
            timeout=timeout)

    def _receipt(self, sale: dict) -> dict:
        # .get() fallbacks keep pre-tax sale dicts printable
        subtotal = sale.get("subtotal", sale["total"])
        tax_total = sale.get("tax_total", 0.0)
        lines = [f"=== SwiftBill receipt ({self.terminal_id}) ==="]
        for line in sale["items"]:
            pid, qty, price = line[0], line[1], line[2]
            tax_rate = line[3] if len(line) > 3 else 0.0
            line_tax = line[4] if len(line) > 4 else 0.0
            lines.append(f"{pid} x{qty} @ ₹{price:.2f} = ₹{qty * price:.2f}")
            if line_tax:
                lines.append(f"  incl. tax {tax_rate:g}% = ₹{line_tax:.2f}")
        lines.append(f"SUBTOTAL: ₹{subtotal:.2f}")
        lines.append(f"TAX: ₹{tax_total:.2f}")
        lines.append(f"TOTAL: ₹{sale['total']:.2f}")
        lines.append(f"txn: {sale['txn_id'][:8]}  idem: {sale['idempotency_key'][:16]}...")
        return {"sale": sale, "text": "\n".join(lines)}

    # -- sync -------------------------------------------------------------------
    def sync_now_async(self):
        return self.scheduler.submit(SYNC, self.sync.sync_once, name="sync")

    def sync_now(self, timeout=60):
        return self.sync_now_async().result(timeout=timeout)

    # -- chatbot ------------------------------------------------------------------
    def ask_async(self, question: str):
        return self.scheduler.submit(CHATBOT, lambda: self.chatbot.ask(question),
                                     name="chatbot")

    def ask(self, question: str, timeout=30) -> str:
        return self.ask_async(question).result(timeout=timeout)
