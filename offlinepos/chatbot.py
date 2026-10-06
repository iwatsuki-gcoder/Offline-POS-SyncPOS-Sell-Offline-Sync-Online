"""Hybrid chatbot assistant.

Offline: lightweight intent matching directly over the local SQLite data.
No model, no network -- answers sales totals, stock checks and sync status.
Online: intended to call an LLM API for open-ended questions. The online
path is a clearly-marked stub (see docs/ARCHITECTURE_DECISIONS.md ADR-002):
if no API key is configured it degrades gracefully instead of failing.

Key design point: the bot subscribes to the *same* Connectivity state
machine as the sync engine, so "offline mode" can never disagree with the
sync engine's view of the world.
"""
from __future__ import annotations

import datetime
import os
import re


class Chatbot:
    def __init__(self, local_db, net, terminal_id: str):
        self.db = local_db
        self.net = net
        self.terminal_id = terminal_id
        self.mode = "offline"
        net.subscribe(self.on_connectivity)
        self.on_connectivity(net.state)

    def on_connectivity(self, state: str):
        self.mode = "online" if state == "online" else "offline"

    # -- entry ------------------------------------------------------------------
    def ask(self, question: str) -> str:
        q = question.strip()
        if self.mode == "online":
            llm = self._ask_llm(q)
            if llm is not None:
                return llm
            # graceful degradation: fall through to local intents
        return self._answer_offline(q)

    # -- online (stub with documented fallback) ------------------------------------
    def _ask_llm(self, question: str) -> str | None:
        api_key = os.environ.get("OFFLINEPOS_LLM_KEY")
        if not api_key:
            return None  # not configured -> degrade to offline intents
        # Production would POST to the configured LLM endpoint here.
        return (f"[online LLM] (stub - no real call made in this build) "
                f"You asked: {question!r}. Configure OFFLINEPOS_LLM_KEY "
                f"and the provider endpoint to enable live answers.")

    # -- offline intents ------------------------------------------------------------
    def _answer_offline(self, q: str) -> str:
        low = q.lower()

        if re.search(r"\bhelp\b", low):
            return ("I work offline. Try: 'total sales today', "
                    "'stock of <product>', 'sync status', 'pending transactions'.")

        if re.search(r"total|sales today|revenue", low):
            start = datetime.datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            total = self.db.sales_total_since(start)
            return f"Today's sales on {self.terminal_id}: ${total:.2f} (offline data)."

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
                            f"{hit['stock']} in stock @ ${hit['price']:.2f}.")
                return f"No product matching '{name}' found locally."
            lines = [f"{p['product_id']}: {p['stock']} @ ${p['price']:.2f}"
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

        return ("I'm offline and didn't understand that. I can answer: "
                "sales totals, stock checks, sync status. (online LLM not "
                "configured - see ADR-002)")
