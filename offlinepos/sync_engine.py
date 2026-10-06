"""Sync engine: delta sync with conflict resolution, idempotency and retry.

Pipeline per sync round:
  1. Snapshot pending fact tables (txns, stock deltas, product updates).
  2. Push to central via ``CentralDB.apply_batch`` with exponential-backoff
     retries. The chaos injector may raise ``ConnectionError`` here.
  3. On success: mark the snapshotted rows synced, record any conflicts the
     central reported into the local ``conflicts`` table + audit log.
  4. Pull: converge local product state toward central (see
     ``LocalDB.apply_central_products``).

Idempotency: steps 2 is safe to retry because the central enforces
``idempotency_key`` / ``applied_deltas`` uniqueness. A retry never creates a
duplicate sale.

Backoff: ``base * 2**attempt + jitter``, capped. Defaults are small so the
test suite runs fast; production would use base=1.0, cap=60.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SyncReport:
    ok: bool = False
    pushed_txns: int = 0
    pushed_deltas: int = 0
    pushed_updates: int = 0
    deduped_txns: int = 0
    deduped_deltas: int = 0
    conflicts: list = field(default_factory=list)
    retries: int = 0
    error: str = ""
    duration_s: float = 0.0


class SyncEngine:
    def __init__(self, local, central, net, terminal_id,
                 chaos=None, backoff_base=0.05, backoff_cap=2.0, clock=None):
        self.local = local
        self.central = central
        self.net = net
        self.terminal_id = terminal_id
        self.chaos = chaos
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.clock = clock or time.time
        self._bg_thread: threading.Thread | None = None
        self._bg_stop = threading.Event()

    # -- one round -----------------------------------------------------------
    def sync_once(self, max_retries=5) -> SyncReport:
        t0 = self.clock()
        rep = SyncReport()
        if not self.net.online:
            rep.error = "offline: sync deferred"
            return rep

        txns = self.local.pending_txns()
        deltas = self.local.pending_deltas()
        updates = self.local.pending_updates()

        attempt = 0
        while True:
            try:
                if self.chaos:
                    self.chaos.maybe_fail("push")
                batch = self.central.apply_batch(
                    product_updates=updates, deltas=deltas, txns=txns)
                # chaos: duplicate the exact same batch -> must be deduped
                if self.chaos and self.chaos.duplicate_push:
                    dup = self.central.apply_batch(
                        product_updates=updates, deltas=deltas, txns=txns)
                    rep.deduped_txns += dup["deduped_txns"]
                    rep.deduped_deltas += dup["deduped_deltas"]
                break
            except ConnectionError as e:
                if attempt >= max_retries:
                    rep.error = f"push failed after {attempt} retries: {e}"
                    rep.duration_s = self.clock() - t0
                    return rep
                delay = min(self.backoff_base * (2 ** attempt)
                            + random.uniform(0, self.backoff_base),
                            self.backoff_cap)
                time.sleep(delay)
                attempt += 1
                rep.retries += 1
            except Exception as e:  # noqa: BLE001 - non-transient, don't retry
                rep.error = f"non-transient failure: {e}"
                rep.duration_s = self.clock() - t0
                return rep

        rep.pushed_txns = batch["applied_txns"]
        rep.pushed_deltas = batch["applied_deltas"]
        rep.pushed_updates = batch["applied_updates"]
        rep.deduped_txns += batch["deduped_txns"]
        rep.deduped_deltas += batch["deduped_deltas"]
        rep.conflicts = batch["conflicts"]

        # mark exactly what we pushed
        self.local.mark_synced("transactions", "txn_id", [t["txn_id"] for t in txns])
        self.local.mark_synced("stock_deltas", "delta_id", [d["delta_id"] for d in deltas])
        self.local.mark_synced("product_updates", "update_id",
                               [u["update_id"] for u in updates])

        for c in rep.conflicts:
            self.local.record_conflict(c["entity"], c["entity_id"],
                                       c["loser"], c["winner"], c["resolution"])
        if rep.conflicts:
            self.local.audit(self.terminal_id, "sync_conflicts",
                             f"{len(rep.conflicts)} conflict(s) resolved")

        # pull: converge local state toward central
        try:
            self.local.apply_central_products(self.central.fetch_products())
        except Exception as e:  # noqa: BLE001 - pull failure is non-fatal
            self.local.audit(self.terminal_id, "pull_failed", str(e))

        self.local.set_kv("last_sync_ts", str(self.clock()))
        self.local.audit(self.terminal_id, "sync",
                         f"txns={rep.pushed_txns} deltas={rep.pushed_deltas} "
                         f"updates={rep.pushed_updates} retries={rep.retries}")
        rep.ok = True
        rep.duration_s = self.clock() - t0
        return rep

    # -- background loop -------------------------------------------------------
    def start_background(self, interval_s=5.0):
        if self._bg_thread and self._bg_thread.is_alive():
            return

        def loop():
            while not self._bg_stop.wait(interval_s):
                if self.net.online:
                    try:
                        self.sync_once()
                    except Exception:
                        pass

        self._bg_stop.clear()
        self._bg_thread = threading.Thread(target=loop, daemon=True,
                                           name=f"sync-{self.terminal_id}")
        self._bg_thread.start()

    def stop_background(self):
        self._bg_stop.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=5)
