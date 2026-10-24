"""Cluster-wide sync: one click converges *every* counter's local database.

Why this exists
---------------
``SyncEngine.sync_once`` pushes one terminal's changes to the central DB and
pulls the central state back into that same terminal only. So when COUNTER-2
sells something and syncs, central is correct, but COUNTER-1's local database
stays stale until somebody clicks sync on COUNTER-1 as well.

``sync_all`` fixes that without changing the engine:

  1. sync the terminal the user clicked (push its sales, pull central),
  2. sync every other terminal (push *their* pending sales, pull central),
  3. pull-only on all but the last terminal, because their first pull
     happened before the later terminals pushed.

After step 3 every online counter holds the same inventory as central, except
for products with unsynced local sales (the engine deliberately never
overwrites those, so no sale is ever lost).
"""
from __future__ import annotations

import time

from .scheduler import SYNC


def pull_now(terminal, timeout: float = 60) -> bool:
    """Pull central -> this terminal's local DB (no push). True on success.

    Runs on the SYNC lane, so it never delays a sale at the BILLING lane.
    """
    if not terminal.net.online:
        return False

    def _pull():
        terminal.db.apply_central_products(terminal.central.fetch_products())
        terminal.db.set_kv("last_sync_ts", str(time.time()))
        return True

    try:
        return bool(terminal.scheduler.submit(SYNC, _pull, name="pull")
                    .result(timeout=timeout))
    except Exception as e:  # noqa: BLE001 - central down etc.; never fatal
        try:
            terminal.db.audit(terminal.terminal_id, "pull_failed", str(e))
        except Exception:
            pass
        return False


def sync_all(terminals: dict, origin: str, timeout: float = 60) -> dict:
    """Sync ``origin`` first, then every other counter, then converge all.

    Returns ``{terminal_id: SyncReport}``; the origin's report is the one
    that matters to the caller who clicked the button.
    """
    order = [origin] + [tid for tid in terminals if tid != origin]
    reports = {tid: terminals[tid].sync_now(timeout=timeout) for tid in order}
    for tid in order[:-1]:
        pull_now(terminals[tid], timeout=timeout)
    return reports