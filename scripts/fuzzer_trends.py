"""Per-scenario fuzzer failure trend tracking and regression detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ScenarioTrend:
    """Trend summary for a single fuzzer scenario."""

    scenario_name: str
    total_runs: int
    failure_count: int
    failure_rate: float
    recent_failures: list[str]
    trend: str  # 'improving', 'stable', 'degrading', 'new'


class FuzzerTrendTracker:
    """Tracks per-scenario failure rates over time to detect regressions."""

    def __init__(self) -> None:
        self._runs: dict[str, list[tuple[str, bool]]] = {}

    def record_run(self, scenario_name: str, timestamp: str, passed: bool) -> None:
        """Record one run outcome for a scenario."""
        self._runs.setdefault(scenario_name, []).append((timestamp, passed))

    def get_trends(self, window_days: int = 14) -> list[ScenarioTrend]:
        """Return trends for all scenarios within the time window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        results: list[ScenarioTrend] = []
        for name, runs in sorted(self._runs.items()):
            windowed = [
                (ts, passed)
                for ts, passed in runs
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff
            ]
            if not windowed:
                continue
            total = len(windowed)
            failures = [(ts, passed) for ts, passed in windowed if not passed]
            failure_count = len(failures)
            failure_rate = failure_count / total if total else 0.0
            recent_failures = [ts for ts, _ in failures]
            trend = self._compute_trend(windowed)
            results.append(
                ScenarioTrend(
                    scenario_name=name,
                    total_runs=total,
                    failure_count=failure_count,
                    failure_rate=failure_rate,
                    recent_failures=recent_failures,
                    trend=trend,
                )
            )
        return results

    def get_degrading_scenarios(self) -> list[ScenarioTrend]:
        """Return only scenarios with 'degrading' trend."""
        return [t for t in self.get_trends() if t.trend == "degrading"]

    @staticmethod
    def _compute_trend(windowed: list[tuple[str, bool]]) -> str:
        """Compare failure rate in recent half vs older half of window."""
        n = len(windowed)
        if n < 2:
            return "new"
        mid = n // 2
        older = windowed[:mid]
        recent = windowed[mid:]
        older_rate = sum(1 for _, p in older if not p) / len(older)
        recent_rate = sum(1 for _, p in recent if not p) / len(recent)
        if recent_rate > older_rate + 0.1:
            return "degrading"
        if recent_rate < older_rate - 0.1:
            return "improving"
        return "stable"
