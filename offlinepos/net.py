"""Connectivity state machine shared by the sync engine and the chatbot.

Both components subscribe to the *same* state instead of tracking their own,
so the chatbot's offline/online behaviour can never disagree with the sync
engine's.

Two modes:
- ``manual`` (default): ``set_online`` is driven by hand — the UI toggle,
  simulations, or the chaos injector. This is the deterministic demo mode.
- ``auto``: a background thread probes real internet reachability every
  ``interval`` seconds and drives the state machine from the result.
  ``set_online`` still works but the next probe may override it.
"""
from __future__ import annotations

import socket
import threading
import time

#: Well-known endpoints used for the reachability probe. Plain TCP (no
#: HTTP, no DNS lookup of our own) so the check is fast and dependency-free.
DEFAULT_ENDPOINTS = (("8.8.8.8", 53), ("1.1.1.1", 443))


def internet_reachable(endpoints=DEFAULT_ENDPOINTS, timeout: float = 3.0) -> bool:
    """True if any probe endpoint accepts a TCP connection.

    ``endpoints`` is injectable so tests can point it at a local socket
    server instead of the real internet.
    """
    for host, port in endpoints:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class Connectivity:
    OFFLINE = "offline"
    ONLINE = "online"

    def __init__(self):
        self._state = self.OFFLINE
        self._lock = threading.Lock()
        self._subs: list = []
        self._mode = "manual"
        self._auto_stop: threading.Event | None = None
        self._auto_thread: threading.Thread | None = None

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_auto_detect(self, enabled: bool, interval: float = 10.0,
                        endpoints=DEFAULT_ENDPOINTS,
                        timeout: float = 3.0) -> str:
        """Switch between 'manual' and 'auto' connectivity modes.

        In auto mode a daemon thread probes ``internet_reachable`` every
        ``interval`` seconds and drives the state machine. Idempotent.
        Returns the new mode.
        """
        with self._lock:
            want = "auto" if enabled else "manual"
            if want == self._mode:
                return self._mode
            self._mode = want
            if self._auto_stop is not None:
                self._auto_stop.set()
                self._auto_stop = None
            if want == "auto":
                stop = self._auto_stop = threading.Event()
                t = self._auto_thread = threading.Thread(
                    target=self._auto_loop,
                    args=(stop, interval, endpoints, timeout),
                    daemon=True, name="net-autodetect")
                t.start()
            return want

    def _auto_loop(self, stop: threading.Event, interval: float,
                   endpoints, timeout: float):
        while not stop.wait(interval):
            try:
                self.set_online(internet_reachable(endpoints, timeout))
            except Exception:
                pass  # a failed probe must never kill the loop

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
