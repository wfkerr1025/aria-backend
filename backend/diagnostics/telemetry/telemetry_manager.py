import time
from collections import deque


class TelemetryManager:
    """
    Unified backend utility for ARIA Lite.
    Tracks:
      - startup durations
      - packet execution times
    Provides rolling metrics for diagnostics panels.
    """

    def __init__(self, max_samples=20):
        self.max_samples = max_samples
        self.startup_times = deque(maxlen=max_samples)
        self.packet_times = deque(maxlen=max_samples)

    # =========================================================
    # STARTUP METRICS
    # =========================================================
    def record_startup(self, duration):
        """Record a startup duration in seconds."""
        if duration is not None:
            self.startup_times.append(float(duration))

    def get_startup_metrics(self):
        """Return min/avg/max startup duration with sample list."""
        if not self.startup_times:
            return {
                "min": None,
                "max": None,
                "avg": None,
                "samples": []
            }

        data = list(self.startup_times)
        return {
            "min": min(data),
            "max": max(data),
            "avg": sum(data) / len(data),
            "samples": data,
        }

    # =========================================================
    # PACKET EXECUTION METRICS
    # =========================================================
    def record_packet_time(self, duration):
        """Record packet execution time in seconds."""
        if duration is not None:
            self.packet_times.append(float(duration))

    def get_packet_metrics(self):
        """Return min/avg/max packet execution time with sample list."""
        if not self.packet_times:
            return {
                "min": None,
                "max": None,
                "avg": None,
                "samples": []
            }

        data = list(self.packet_times)
        return {
            "min": min(data),
            "max": max(data),
            "avg": sum(data) / len(data),
            "samples": data,
        }

    # =========================================================
    # TIMESTAMP UTILITY
    # =========================================================
    def now(self):
        """Convenience timestamp."""
        return time.time()
