"""Priority-based scheduling (OS concept).

Rule: **billing never waits on background work.**

Implementation: two lanes.
- A dedicated billing worker (FIFO) that only ever runs checkout tasks.
  Even if the background lane is saturated with sync/retry jobs, a sale at
  the counter starts immediately.
- A background lane: single worker draining a priority heap
  (CHATBOT < SYNC < RETRY < MAINTENANCE, lower number = higher priority).

Wait-time statistics are recorded per lane so simulations can prove the
billing lane is starvation-free.
"""
from __future__ import annotations

import heapq
import itertools
import queue
import threading
import time
from concurrent.futures import Future

BILLING = 0
CHATBOT = 1
SYNC = 2
RETRY = 3
MAINTENANCE = 4

_NAMES = {BILLING: "BILLING", CHATBOT: "CHATBOT", SYNC: "SYNC",
          RETRY: "RETRY", MAINTENANCE: "MAINTENANCE"}


class PriorityScheduler:
    def __init__(self):
        self._billing_q: queue.Queue = queue.Queue()
        self._bg_heap: list = []
        self._bg_seq = itertools.count()
        self._bg_cv = threading.Condition()
        self._stop = False
        self._stats_lock = threading.Lock()
        self._wait_s: dict[str, list[float]] = {n: [] for n in _NAMES.values()}
        self._t_billing = threading.Thread(target=self._billing_loop,
                                           name="billing-worker", daemon=True)
        self._t_bg = threading.Thread(target=self._bg_loop,
                                      name="bg-worker", daemon=True)

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        self._t_billing.start()
        self._t_bg.start()

    def stop(self):
        self._stop = True
        self._billing_q.put(None)
        with self._bg_cv:
            self._bg_cv.notify_all()
        self._t_billing.join(timeout=5)
        self._t_bg.join(timeout=5)

    # -- submit --------------------------------------------------------------
    def submit(self, priority: int, fn, name: str = "") -> Future:
        fut: Future = Future()
        enqueued = time.monotonic()

        def record_and_run():
            with self._stats_lock:
                self._wait_s[_NAMES[priority]].append(time.monotonic() - enqueued)
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

        if priority == BILLING:
            self._billing_q.put(record_and_run)
        else:
            with self._bg_cv:
                heapq.heappush(self._bg_heap,
                               (priority, next(self._bg_seq), record_and_run))
                self._bg_cv.notify()
        return fut

    # -- workers --------------------------------------------------------------
    def _billing_loop(self):
        while True:
            job = self._billing_q.get()
            if job is None or self._stop:
                return
            job()

    def _bg_loop(self):
        while True:
            with self._bg_cv:
                while not self._bg_heap and not self._stop:
                    self._bg_cv.wait()
                if self._stop:
                    return
                _, _, job = heapq.heappop(self._bg_heap)
            job()

    # -- stats -----------------------------------------------------------------
    def wait_stats(self) -> dict:
        with self._stats_lock:
            out = {}
            for lane, samples in self._wait_s.items():
                if samples:
                    s = sorted(samples)
                    out[lane] = {"count": len(s),
                                 "p50": round(s[len(s) // 2], 4),
                                 "max": round(s[-1], 4)}
                else:
                    out[lane] = {"count": 0, "p50": 0.0, "max": 0.0}
            return out
