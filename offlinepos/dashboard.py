"""Terminal health dashboard (text render)."""
from __future__ import annotations

import datetime


def render_dashboard(local_db, terminal_id: str, net) -> str:
    counts = local_db.pending_counts()
    conflicts = local_db.list_conflicts()
    last = local_db.get_kv("last_sync_ts")
    last_s = ("never" if not last else
              datetime.datetime.fromtimestamp(float(last)).strftime("%Y-%m-%d %H:%M:%S"))
    products = local_db.list_products()
    state = "ONLINE" if net.online else "OFFLINE"

    lines = [
        f"--- SwiftBill health dashboard [{terminal_id}] [{state}] ---",
        f"last sync      : {last_s}",
        f"pending        : txns={counts['txns']} deltas={counts['deltas']} "
        f"updates={counts['updates']}",
        f"conflicts logged: {len(conflicts)}",
        "products:",
    ]
    for p in products:
        lines.append(f"  {p['product_id']:8} {p['name'][:20]:20} "
                     f"stock={p['stock']:4} price=₹{p['price']:.2f}")
    if conflicts:
        lines.append("recent conflicts:")
        for c in conflicts[-3:]:
            lines.append(f"  {c['entity']}/{c['entity_id']}: {c['resolution']}")
    return "\n".join(lines)
