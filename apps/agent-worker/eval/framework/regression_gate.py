"""RAG 数据飞轮的确定性发布门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricGate:
    metric_id: str
    minimum: float
    maximum_regression: float = 0.0


@dataclass(frozen=True, slots=True)
class FlywheelGatePolicy:
    gate_id: str
    minimum_sample_count: int
    metrics: tuple[MetricGate, ...]
    maximum_failure_rates: dict[str, float]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FlywheelGatePolicy:
        if value.get("schema_version") != 1:
            raise ValueError("飞轮门禁策略 schema_version 必须为 1")
        gate_id = str(value.get("gate_id", "")).strip()
        minimum_sample_count = value.get("minimum_sample_count")
        raw_metrics = value.get("metrics")
        raw_failures = value.get("maximum_failure_rates", {})
        if not gate_id or not isinstance(minimum_sample_count, int) or minimum_sample_count < 1:
            raise ValueError("飞轮门禁策略缺少有效 gate_id/minimum_sample_count")
        if not isinstance(raw_metrics, dict) or not raw_metrics:
            raise ValueError("飞轮门禁策略至少需要一个 metric")
        if not isinstance(raw_failures, dict):
            raise ValueError("maximum_failure_rates 必须是对象")
        metrics = []
        for metric_id, rule in raw_metrics.items():
            if not isinstance(rule, dict):
                raise ValueError(f"指标门禁规则无效: {metric_id}")
            minimum = _ratio(rule.get("minimum"), f"{metric_id}.minimum")
            regression = _ratio(
                rule.get("maximum_regression", 0.0),
                f"{metric_id}.maximum_regression",
            )
            metrics.append(MetricGate(str(metric_id), minimum, regression))
        failure_rates = {
            str(failure_type): _ratio(limit, f"{failure_type}.maximum_failure_rate")
            for failure_type, limit in raw_failures.items()
        }
        return cls(gate_id, minimum_sample_count, tuple(metrics), failure_rates)


def evaluate_regression_gate(
    report: dict[str, Any],
    policy: FlywheelGatePolicy,
    *,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只消费脱敏聚合报告; 没有足够晋升样本时绝不误报通过。"""

    sample_count = int(report.get("sample_count", 0))
    current_metrics = report.get("aggregate_metrics", {})
    baseline_metrics = (baseline or {}).get("aggregate_metrics", {})
    failures = report.get("failure_counts", {})
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "check_id": "minimum_sample_count",
            "passed": sample_count >= policy.minimum_sample_count,
            "actual": sample_count,
            "required": policy.minimum_sample_count,
        }
    )
    for rule in policy.metrics:
        actual = current_metrics.get(rule.metric_id)
        baseline_value = baseline_metrics.get(rule.metric_id)
        floor = rule.minimum
        if isinstance(baseline_value, int | float):
            floor = max(floor, float(baseline_value) - rule.maximum_regression)
        checks.append(
            {
                "check_id": f"metric:{rule.metric_id}",
                "passed": isinstance(actual, int | float) and float(actual) >= floor,
                "actual": actual,
                "required_minimum": floor,
                "absolute_minimum": rule.minimum,
                "baseline": baseline_value,
                "maximum_regression": rule.maximum_regression,
            }
        )
    denominator = max(sample_count, 1)
    for failure_type, maximum in sorted(policy.maximum_failure_rates.items()):
        count = int(failures.get(failure_type, 0))
        rate = count / denominator
        checks.append(
            {
                "check_id": f"failure_rate:{failure_type}",
                "passed": rate <= maximum,
                "actual": rate,
                "failure_count": count,
                "maximum": maximum,
            }
        )

    failed = [check["check_id"] for check in checks if not check["passed"]]
    status = "passed" if not failed else "blocked"
    if sample_count < policy.minimum_sample_count:
        status = "insufficient_evidence"
    return {
        "schema_version": 1,
        "gate_id": policy.gate_id,
        "status": status,
        "sample_count": sample_count,
        "failed_checks": failed,
        "checks": checks,
    }


def build_review_queue(rows, report: dict[str, Any], *, limit: int = 100) -> list[dict]:
    """自动挖掘待审核项; 但不自动批准隐私、生成金标或晋升。"""

    failures_by_trace: dict[str, list[dict]] = {}
    for sample in report.get("samples", []):
        failures_by_trace[str(sample["trace_id"])] = list(sample.get("failures", []))

    values = []
    for trace, label in rows:
        reasons: list[str] = []
        severity = 0
        if trace.privacy_status == "pending":
            reasons.append("privacy_review_required")
            severity = max(severity, 3)
        elif trace.privacy_status == "approved" and label is None:
            reasons.append("evidence_label_required")
            severity = max(severity, 2)
        elif label is not None and label.status == "draft":
            reasons.append("label_confirmation_required")
            severity = max(severity, 2)
        elif label is not None and label.status == "confirmed":
            reasons.append("promotion_review_required")
            severity = max(severity, 1)
        if trace.result_count == 0:
            reasons.append("empty_context")
            severity = max(severity, 4)
        if trace.context_truncated:
            reasons.append("context_truncated")
            severity = max(severity, 2)
        execution = str(trace.pipeline_versions.get("reranker_execution", ""))
        if ":degraded:" in execution or ":failed:" in execution:
            reasons.append("reranker_degraded")
            severity = max(severity, 3)
        failures = failures_by_trace.get(str(trace.id), [])
        if failures:
            reasons.extend(f"failure:{value['failure_type']}" for value in failures)
            severity = max(severity, 4)
        if reasons:
            values.append(
                {
                    "trace_id": str(trace.id),
                    "query_hash": trace.query_hash,
                    "created_at": trace.created_at.isoformat(),
                    "privacy_status": trace.privacy_status,
                    "label_status": label.status if label else None,
                    "priority": severity,
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
    values.sort(key=lambda value: (-value["priority"], value["created_at"], value["trace_id"]))
    return values[: min(max(limit, 1), 500)]


def _ratio(value: Any, field: str) -> float:
    if not isinstance(value, int | float) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field} 必须在 0..1")
    return float(value)
