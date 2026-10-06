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
        # chatbot follows the same connectivity state machine as sync
        self.net.subscribe(lambda state: self.chatbot.on_connectivity(state))

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self.scheduler.start()

    def close(self):
        self.sync.stop_background()
        self.scheduler.stop()

    def set_online(self, value: bool):
        self.net.set_online(value)

    # -- catalog --------------------------------------------------------------
    def seed_catalog(self, items: list[tuple[str, str, float, int]]):
        for pid, name, price, stock in items:
            self.db.seed_product(pid, name, price, stock, self.terminal_id)

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

    def _receipt(self, sale: dict) -> dict:
        lines = [f"=== OfflinePOS receipt ({self.terminal_id}) ==="]
        for pid, qty, price in sale["items"]:
            lines.append(f"{pid} x{qty} @ ${price:.2f} = ${qty * price:.2f}")
        lines.append(f"TOTAL: ${sale['total']:.2f}")
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
