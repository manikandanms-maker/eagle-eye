"""Runs every rule, aggregates findings, computes the verdict and ₹ at risk."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config as C
from .loader import collect
from .rules import ALL_RULES, Finding
from .schema import Order


@dataclass
class AuditResult:
    order: Order
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    @property
    def verdict(self) -> str:
        flag_rank = C.SEVERITY_ORDER[C.FLAG_AT]
        worst = max((C.SEVERITY_ORDER.get(f.severity, 0) for f in self.failures), default=0)
        return "FLAG" if worst >= flag_rank else "PASS"

    @property
    def rupees_at_risk(self) -> float:
        return round(sum(f.rupee_impact for f in self.failures), 2)

    @property
    def top_severity(self) -> str:
        if not self.failures:
            return "NONE"
        return max(self.failures, key=lambda f: C.SEVERITY_ORDER.get(f.severity, 0)).severity

    def sorted_failures(self) -> list[Finding]:
        return sorted(self.failures,
                      key=lambda f: (C.SEVERITY_ORDER.get(f.severity, 0), f.rupee_impact),
                      reverse=True)

    def summary(self) -> dict:
        return {
            "order_id": self.order.order_id,
            "order_type": self.order.order_type,
            "verdict": self.verdict,
            "top_severity": self.top_severity,
            "rupees_at_risk": self.rupees_at_risk,
            "failure_count": len(self.failures),
            "checks_run": len(self.findings) + len([1 for _ in []]),  # findings includes passes
        }


def run_rules(order: Order) -> AuditResult:
    findings: list[Finding] = []
    for rule in ALL_RULES:
        try:
            produced = rule(order) or []
        except Exception as exc:  # a buggy rule must never kill the audit
            produced = [Finding(
                module="ENGINE", rule_id=getattr(rule, "__name__", "rule"),
                title="Rule error", status="FAIL", severity="LOW",
                detail=f"Rule raised: {exc}")]
        if produced:
            findings.extend(produced)
        # rules that return [] are passing checks; we don't clutter output with them
    return AuditResult(order=order, findings=findings)


def audit(order_id: str, source: str = "auto") -> AuditResult:
    """Full pipeline entry point: collect -> run deterministic rules."""
    order = collect(order_id, source=source)
    return run_rules(order)
