"""Chaos injector for sync-failure testing.

Failure modes (see docs/CHAOS_TESTING.md):
- ``fail_push_times``: drop the connection mid-push N times (transient).
- ``duplicate_push``: send the same batch twice (tests idempotency).
- clock skew is simulated by offsetting ``LocalDB.clock`` in the scenario.
"""
from __future__ import annotations


class Chaos:
    def __init__(self):
        self.fail_push_times = 0
        self.duplicate_push = False
        self.drops_seen = 0

    def maybe_fail(self, stage: str):
        if self.fail_push_times > 0:
            self.fail_push_times -= 1
            self.drops_seen += 1
            raise ConnectionError(f"chaos: connection dropped during {stage}")
