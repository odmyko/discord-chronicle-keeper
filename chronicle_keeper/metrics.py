from __future__ import annotations

from dataclasses import dataclass, asdict
from threading import Lock
from typing import Dict


@dataclass
class StageMetrics:
    calls: int = 0
    errors: int = 0
    total_latency_s: float = 0.0
    max_latency_s: float = 0.0

    def avg_latency_s(self) -> float:
        if self.calls <= 0:
            return 0.0
        return self.total_latency_s / self.calls


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._stages: Dict[str, StageMetrics] = {}

    def observe(self, stage: str, duration_s: float, ok: bool) -> None:
        safe_stage = stage.strip() or "unknown"
        safe_duration = max(0.0, float(duration_s))
        with self._lock:
            current = self._stages.get(safe_stage)
            if current is None:
                current = StageMetrics()
                self._stages[safe_stage] = current
            current.calls += 1
            if not ok:
                current.errors += 1
            current.total_latency_s += safe_duration
            if safe_duration > current.max_latency_s:
                current.max_latency_s = safe_duration

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                stage: {
                    **asdict(metric),
                    "avg_latency_s": metric.avg_latency_s(),
                }
                for stage, metric in self._stages.items()
            }
