#!/usr/bin/env python3
"""Eagle Eye CLI — zero-dependency demo.

Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.

    python cli.py                 # list available order IDs
    python cli.py CL-ORD-1002     # audit one order
    python cli.py CL-ORD-1002 --json

Works with no API key and no pip installs (pure stdlib). Set ANTHROPIC_API_KEY
to turn on Claude narration.
"""
import json
import sys

from audit.engine import audit
from audit.loader import list_order_ids
from audit.reasoner import explain

# ANSI colors (degrade gracefully if piped)
G, R, Y, B, DIM, BOLD, END = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[2m", "\033[1m", "\033[0m"
SEV_COLOR = {"CRITICAL": R, "HIGH": R, "MEDIUM": Y, "LOW": DIM, "INFO": DIM}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv

    if not args:
        ids = list_order_ids()
        print(f"{BOLD}Available orders:{END} " + ", ".join(ids))
        print(f"{DIM}Usage: python cli.py <ORDER_ID>{END}")
        return 0

    order_id = args[0]
    try:
        result = audit(order_id)
    except KeyError as e:
        print(f"{R}{e}{END}")
        return 1

    narrative = explain(result)

    if as_json:
        print(json.dumps({"summary": result.summary(), "narrative": narrative}, indent=2, default=str))
        return 0

    color = G if result.verdict == "PASS" else R
    print()
    print(f"{BOLD}🦅 Eagle Eye audit · {order_id} ({result.order.order_type}){END}")
    print(f"{BOLD}│ Verdict: {color}{result.verdict}{END}  "
          f"{BOLD}₹ at risk: {color}{result.rupees_at_risk:,.2f}{END}  "
          f"{DIM}top severity: {result.top_severity}{END}")
    print(f"{BOLD}└─{END} {narrative.get('verdict','')}")
    print(f"{DIM}   reasoner: {narrative.get('engine','offline')}{END}\n")

    issues = narrative.get("issues", [])
    if not issues:
        print(f"{G}✓ All checks passed — no revenue leakage detected.{END}")
    for i, issue in enumerate(issues, 1):
        sev = issue.get("severity", "INFO")
        c = SEV_COLOR.get(sev, DIM)
        impact = issue.get("rupee_impact") or 0
        line = f"  · {issue['line']}" if issue.get("line") else ""
        print(f"{c}{i}. [{sev}] {issue['title']}{END}  "
              f"{DIM}(₹{impact:,.2f}){END}{line}")
        print(f"   {issue.get('explanation','')}")
        print(f"   {B}fix:{END} {issue.get('fix','')}\n")

    for a in narrative.get("anomalies", []):
        print(f"{Y}⚠ anomaly: {a}{END}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
