"""Chatbot assistant: offline intent matching over the local SQLite data.

No model, no network — answers sales totals, stock checks and sync
status from the terminal's own database, instantly. The assistant works
exactly like billing does: with zero connectivity. It still reads the
shared connectivity state machine so its "sync status" answer can never
disagree with the sync engine's view of the world.
"""
from __future__ import annotations

import datetime
import re


class Chatbot:
    def __init__(self, local_db, net, terminal_id: str):
        self.db = local_db
        self.net = net
        self.terminal_id = terminal_id

    # -- entry ------------------------------------------------------------------
    def ask(self, question: str) -> str:
        return self._answer_offline(question.strip())

    # -- offline intents ------------------------------------------------------------
    def _answer_offline(self, q: str) -> str:
        low = q.lower()

        if re.search(r"\bhelp\b", low):
            return ("Try: 'total sales today', "
                    "'stock of <product>', 'sync status', 'pending transactions'.")

        if re.search(r"total|sales today|revenue", low):
            start = datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            total = self.db.sales_total_since(start)
            return f"Today's sales on {self.terminal_id}: ₹{total:.2f} (local data)."

        m = re.search(r"stock of ([\w\- ]+?)(?:\?|$)", low)
        if m or "stock" in low:
            name = m.group(1).strip() if m else ""
            products = self.db.list_products()
            if name:
                hit = next((p for p in products
                            if name in p["name"].lower() or name == p["product_id"].lower()),
                           None)
                if hit:
                    return (f"{hit['name']} ({hit['product_id']}): "
                            f"{hit['stock']} in stock @ ₹{hit['price']:.2f}.")
                return f"No product matching '{name}' found locally."
            lines = [f"{p['product_id']}: {p['stock']} @ ₹{p['price']:.2f}"
                     for p in products]
            return "Stock levels:\n" + "\n".join(lines)

        if re.search(r"sync|pending", low):
            counts = self.db.pending_counts()
            last = self.db.get_kv("last_sync_ts")
            last_s = ("never" if not last else
                      datetime.datetime.fromtimestamp(float(last)).strftime("%H:%M:%S"))
            total_pending = sum(counts.values())
            state = "ONLINE" if self.net.online else "OFFLINE"
            return (f"Sync status [{state}]: {total_pending} pending "
                    f"(txns={counts['txns']}, deltas={counts['deltas']}, "
                    f"updates={counts['updates']}), last sync: {last_s}.")

        return ("I didn't understand that. I can answer: "
                "sales totals, stock checks, sync status.")
