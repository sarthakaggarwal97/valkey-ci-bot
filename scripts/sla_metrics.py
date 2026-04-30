"""SLA and cost metrics tracking for the CI agent dashboard.

Tracks time-to-fix, review latency, and token spend per operation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class OperationMetric:
    """A single timed operation with cost."""

    operation: str  # "rca", "fix_gen", "review", "backport"
    started_at: str
    completed_at: str
    duration_seconds: float
    tokens_used: int = 0
    success: bool = True


@dataclass
class SLAMetrics:
    """Aggregated SLA metrics for dashboard display."""

    total_operations: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_duration_seconds: float = 0.0
    p50_duration_seconds: float = 0.0
    p95_duration_seconds: float = 0.0
    total_tokens: int = 0
    avg_tokens_per_op: float = 0.0


class MetricsTracker:
    """Tracks operation metrics for SLA and cost reporting."""

    def __init__(self) -> None:
        self._metrics: list[OperationMetric] = []

    def record(self, metric: OperationMetric) -> None:
        self._metrics.append(metric)

    def start_timer(self, operation: str) -> _Timer:
        return _Timer(self, operation)

    def get_sla_metrics(self, operation: str | None = None) -> SLAMetrics:
        """Get aggregated metrics, optionally filtered by operation type."""
        ops = self._metrics
        if operation:
            ops = [m for m in ops if m.operation == operation]
        if not ops:
            return SLAMetrics()

        durations = sorted(m.duration_seconds for m in ops)
        tokens = [m.tokens_used for m in ops]
        successes = sum(1 for m in ops if m.success)

        return SLAMetrics(
            total_operations=len(ops),
            success_count=successes,
            failure_count=len(ops) - successes,
            avg_duration_seconds=sum(durations) / len(durations),
            p50_duration_seconds=durations[len(durations) // 2],
            p95_duration_seconds=durations[int(len(durations) * 0.95)],
            total_tokens=sum(tokens),
            avg_tokens_per_op=sum(tokens) / len(ops) if ops else 0,
        )

    def get_cost_summary(self) -> dict[str, int]:
        """Return total tokens by operation type."""
        by_op: dict[str, int] = {}
        for m in self._metrics:
            by_op[m.operation] = by_op.get(m.operation, 0) + m.tokens_used
        return by_op

    def to_dict(self) -> list[dict]:
        """Serialize all metrics for dashboard JSON."""
        return [
            {
                "operation": m.operation,
                "started_at": m.started_at,
                "completed_at": m.completed_at,
                "duration_seconds": m.duration_seconds,
                "tokens_used": m.tokens_used,
                "success": m.success,
            }
            for m in self._metrics
        ]


class _Timer:
    """Context manager for timing operations."""

    def __init__(self, tracker: MetricsTracker, operation: str) -> None:
        self._tracker = tracker
        self._operation = operation
        self._start = datetime.now(timezone.utc)
        self.tokens_used = 0
        self.success = True

    def __enter__(self) -> _Timer:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        end = datetime.now(timezone.utc)
        self._tracker.record(OperationMetric(
            operation=self._operation,
            started_at=self._start.isoformat(),
            completed_at=end.isoformat(),
            duration_seconds=(end - self._start).total_seconds(),
            tokens_used=self.tokens_used,
            success=self.success if exc_type is None else False,
        ))
