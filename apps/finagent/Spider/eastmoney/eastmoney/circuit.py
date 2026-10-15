"""Thread-safe transport circuit, scoped to one exact upstream endpoint."""
from collections import defaultdict
import threading
import time


class EndpointCircuit:
    def __init__(self, failures=3, cooldown=300, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = failures, cooldown, clock
        self.lock = threading.Lock()
        self.states = defaultdict(lambda: {"failures": 0, "retry_at": 0.0,
                                          "probe": False, "attempted": 0,
                                          "suppressed": 0, "transport_errors": 0})

    def allow(self, endpoint):
        with self.lock:
            state = self.states[endpoint]
            if state["retry_at"]:
                if self.clock() < state["retry_at"] or state["probe"]:
                    state["suppressed"] += 1
                    return False
                state["probe"] = True  # Only one recovery probe after cooldown.
            state["attempted"] += 1
            return True

    def record(self, endpoint, transport_ok):
        with self.lock:
            state = self.states[endpoint]
            state["probe"] = False
            if transport_ok:
                state["failures"], state["retry_at"] = 0, 0.0
            else:
                state["failures"] += 1
                state["transport_errors"] += 1
                if state["failures"] >= self.threshold:
                    state["retry_at"] = self.clock() + self.cooldown

    def snapshot(self):
        with self.lock:
            return {key: {**value, "open": bool(value["retry_at"] and self.clock() < value["retry_at"])}
                    for key, value in self.states.items()}
