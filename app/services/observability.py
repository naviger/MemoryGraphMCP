from __future__ import annotations

from collections import defaultdict


class MemoryObservability:
    def __init__(self) -> None:
        self._metrics: dict[str, dict[str, object]] = defaultdict(
            lambda: {
                "success_count": 0,
                "failure_count": 0,
                "request_count": 0,
                "fallback_count": 0,
                "latencies": [],
                "failure_types": defaultdict(int),
            }
        )
        self._ready = False

    def record_request(self, operation: str) -> None:
        metric = self._metrics[operation]
        metric["request_count"] = int(metric["request_count"]) + 1

    def record_success(self, operation: str, latency_seconds: float) -> None:
        metric = self._metrics[operation]
        metric["success_count"] = int(metric["success_count"]) + 1
        metric["latencies"].append(latency_seconds * 1000)

    def record_failure(self, operation: str, failure_type: str) -> None:
        metric = self._metrics[operation]
        metric["failure_count"] = int(metric["failure_count"]) + 1
        metric["failure_types"][failure_type] = int(metric["failure_types"].get(failure_type, 0)) + 1

    def record_fallback(self, operation: str) -> None:
        metric = self._metrics[operation]
        metric["fallback_count"] = int(metric["fallback_count"]) + 1

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    def readiness_status(self) -> str:
        return "ready" if self._ready else "not_ready"

    def snapshot(self) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for name, metric in self._metrics.items():
            latencies = list(metric["latencies"])
            snapshot[name] = {
                "success_count": int(metric["success_count"]),
                "failure_count": int(metric["failure_count"]),
                "request_count": int(metric["request_count"]),
                "fallback_count": int(metric["fallback_count"]),
                "failure_types": dict(metric["failure_types"]),
                "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            }
        return snapshot
