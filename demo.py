#!/usr/bin/env python3
"""OfflinePOS end-to-end demo.

Run:  python3 demo.py        (or press F5 in VS Code)

Walks through: offline sales -> offline chatbot -> reconnect + sync ->
health dashboard -> conflict resolution. All data lives in a temp dir
printed at the end.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from offlinepos.central_db import CentralDB
from offlinepos.dashboard import render_dashboard
from offlinepos.pos import Terminal


def banner(t):
    print("\n" + "=" * 60 + f"\n{t}\n" + "=" * 60)


def main():
    work = tempfile.mkdtemp(prefix="offlinepos_demo_")
    central = CentralDB(os.path.join(work, "central.db"))
    a = Terminal("COUNTER-1", os.path.join(work, "t1"), central)
    b = Terminal("COUNTER-2", os.path.join(work, "t2"), central)
    for t in (a, b):
        t.start()
    try:
        catalog = [("W1", "Widget", 10.0, 20),
                   ("G1", "Gadget", 25.5, 15),
                   ("S1", "Sprocket", 5.0, 100)]
        a.seed_catalog(catalog)
        b.seed_catalog(catalog)
        for pid, name, price, stock in catalog:
            central.seed_product(pid, name, price, stock)

        banner("1. OFFLINE SALES  (internet is down)")
        a.set_online(False)
        b.set_online(False)
        a.scan("W1", 2)
        a.scan("G1", 1)
        print(a.checkout()["text"])
        b.scan("S1", 5)
        print(b.checkout()["text"])

        banner("2. CHATBOT  (offline mode, no LLM)")
        for q in ("total sales today", "stock of Widget", "sync status"):
            print(f"Q: {q}\nA: {a.ask(q)}\n")

        banner("3. BACK ONLINE -> SYNC")
        a.set_online(True)
        b.set_online(True)
        ra, rb = a.sync_now(), b.sync_now()
        print(f"COUNTER-1 pushed {ra.pushed_txns} txn(s), "
              f"{ra.pushed_deltas} delta(s), {ra.pushed_updates} update(s)")
        print(f"COUNTER-2 pushed {rb.pushed_txns} txn(s), "
              f"{rb.pushed_deltas} delta(s), {rb.pushed_updates} update(s)")
        print(f"Central now holds {central.txn_count()} transaction(s).")

        banner("4. HEALTH DASHBOARD")
        print(render_dashboard(a.db, "COUNTER-1", a.net))

        banner("5. CONFLICT DEMO  (both change Gadget price offline)")
        a.set_online(False)
        b.set_online(False)
        base = time.time()
        a.db.update_product_price("G1", 27.0, "COUNTER-1", ts=base + 1)
        b.db.update_product_price("G1", 29.0, "COUNTER-2", ts=base + 2)
        a.set_online(True)
        b.set_online(True)
        a.sync_now()
        b.sync_now()
        print(f"Central Gadget price: ${central.get_product('G1')['price']:.2f} "
              f"(newer write won last-write-wins)")
        print(f"Conflicts logged for review: {len(b.db.list_conflicts())}")

        banner("DEMO COMPLETE")
        print("All demo data lives in:", work)
        print("Tip: run  python3 simulations/run_all.py  for the full test suite.")
    finally:
        a.close()
        b.close()


if __name__ == "__main__":
    main()
