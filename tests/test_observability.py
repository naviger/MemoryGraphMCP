from __future__ import annotations

from app.services.observability import MemoryObservability


def test_observability_tracks_health_and_metrics() -> None:
    service = MemoryObservability()

    service.record_success("search", 0.14)
    service.record_failure("ingest", "timeout")

    metrics = service.snapshot()

    assert metrics["search"]["success_count"] == 1
    assert metrics["search"]["avg_latency_ms"] == 140.0
    assert metrics["ingest"]["failure_count"] == 1
    assert metrics["ingest"]["failure_types"]["timeout"] == 1


def test_observability_reports_ready_state() -> None:
    service = MemoryObservability()

    assert service.readiness_status() == "not_ready"

    service.set_ready(True)
    assert service.readiness_status() == "ready"
