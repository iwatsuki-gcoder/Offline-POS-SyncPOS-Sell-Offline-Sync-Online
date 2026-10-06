"""Connectivity state machine shared by the sync engine and the chatbot.

Both components subscribe to the *same* state instead of tracking their own,
so the chatbot's offline/online behaviour can never disagree with the sync
engine's. In production ``set_online`` would be driven by a socket probe;
in simulations and tests it is driven manually (or by the chaos injector).
"""
from __future__ import annotations

import threading


class Connectivity:
    OFFLINE = "offline"
    ONLINE = "online"

    def __init__(self):
        self._state = self.OFFLINE
        self._lock = threading.Lock()
        self._subs: list = []

    @property
    def online(self) -> bool:
        with self._lock:
            return self._state == self.ONLINE

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def set_online(self, value: bool):
        with self._lock:
            new = self.ONLINE if value else self.OFFLINE
            if new == self._state:
                return
            self._state = new
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(new)
            except Exception:
                pass  # a subscriber must never break the state machine

    def subscribe(self, callback):
        """callback(state: str)"""
        with self._lock:
            self._subs.append(callback)
