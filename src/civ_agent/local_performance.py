"""Small bounded local timing aggregates; no transport or credentials."""

import threading
from collections import Counter, deque
from pathlib import Path


def memory_usage(*, status=Path("/proc/self/status"), cgroup=Path("/sys/fs/cgroup")):
    """Content-free Linux memory counters; unavailable counters are optional."""
    result = {}
    try:
        names = {"VmRSS:": "residentBytes", "VmHWM:": "peakResidentBytes"}
        for line in status.read_text().splitlines():
            fields = line.split()
            if fields and fields[0] in names:
                result[names[fields[0]]] = int(fields[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    for filename, name in (
        ("memory.current", "cgroupCurrentBytes"),
        ("memory.peak", "cgroupPeakBytes"),
        ("memory.max", "cgroupLimitBytes"),
    ):
        try:
            result[name] = int((cgroup / filename).read_text().strip())
        except (OSError, ValueError):
            pass
    return result


class LocalPerformance:
    def __init__(self, *, samples=256):
        self.limit = samples
        self._samples = {}
        self._counts = Counter()
        self._lock = threading.Lock()

    def count(self, name, amount=1):
        with self._lock:
            self._counts[name] += amount

    def timing(self, name, seconds):
        with self._lock:
            self._samples.setdefault(name, deque(maxlen=self.limit)).append(max(0.0, seconds) * 1000)
            self._counts[name + "Samples"] += 1

    def snapshot(self):
        with self._lock:
            timings = {}
            for name, values in self._samples.items():
                ordered = sorted(values)
                timings[name] = {
                    "retainedSamples": len(ordered),
                    "p50Ms": ordered[(len(ordered) - 1) // 2],
                    "p95Ms": ordered[int((len(ordered) - 1) * 0.95)],
                    "maxMs": ordered[-1],
                }
            return {"counts": dict(self._counts), "timings": timings, "retainedSamplesPerStage": self.limit}
