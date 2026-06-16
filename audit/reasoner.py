"""Claude reasoner — turns deterministic findings into an auditor's narrative.

CRITICAL DESIGN RULE: Claude does NOT decide pass/fail or compute money. The engine
already did that. Claude only (a) writes a crisp human explanation, (b) ranks the
top risks, and (c) does an anomaly catch-all over free-text fields.

If no ANTHROPIC_API_KEY is set (or the SDK isn't installed), we fall back to a
template reasoner so the demo always runs at zero cost.
"""
from __future__ import annotations

import json
import os

from .engine import AuditResult

MODEL = os.getenv("EAGLEEYE_MODEL", os.getenv("ORDERGUARD_MODEL", "claude-haiku-4-5"))

SYSTEM = (
    "You are Eagle Eye, a revenue-assurance auditor for CaratLane (jewellery retail). "
    "You are given an order summary and a list of DETERMINISTIC findings already "
    "computed by a rule engine. Do NOT recompute numbers or change any verdict — trust "
    "the findings. Your job: (1) write a 1-2 sentence executive verdict, (2) explain the "
    "top issues in plain business language a store/ops manager understands, naming the "
    "rupee impact, (3) suggest a concrete fix per issue, and (4) flag any anomaly you "
    "notice in free-text fields (gift_message, special_instruction) that the rules might "
    "have missed. Be concise. Return STRICT JSON only."
)


def _payload(result: AuditResult) -> dict:
    o = result.order
    return {
        "order_id": o.order_id,
        "order_type": o.order_type,
        "verdict": result.verdict,
        "rupees_at_risk": result.rupees_at_risk,
        "free_text": {
            "gift_message": o.header.get("gift_message"),
            "special_instruction": o.header.get("special_instruction"),
        },
        "findings": [
            {
                "module": f.module, "rule_id": f.rule_id, "severity": f.severity,
                "title": f.title, "expected": f.expected, "actual": f.actual,
                "rupee_impact": f.rupee_impact, "line": f.line, "detail": f.detail,
            }
            for f in result.sorted_failures()
        ],
    }


def _offline(result: AuditResult) -> dict:
    """Template narrative — no API, no cost. Good enough to demo on its own."""
    fails = result.sorted_failures()
    if result.verdict == "PASS":
        verdict = (f"Order {result.order.order_id} passes all "
                   f"{len(result.findings) or 'audit'} checks. No revenue leakage detected.")
    else:
        verdict = (f"Order {result.order.order_id} is FLAGGED: {len(fails)} issue(s), "
                   f"₹{result.rupees_at_risk:,.2f} at risk. Top severity {result.top_severity}.")
    issues = []
    for f in fails:
        issues.append({
            "title": f.title,
            "severity": f.severity,
            "rupee_impact": f.rupee_impact,
            "explanation": f.detail or f.title,
            "fix": _suggest_fix(f),
            "line": f.line,
        })
    return {"verdict": verdict, "issues": issues, "anomalies": [], "engine": "offline-fallback"}


def _suggest_fix(f) -> str:
    table = {
        "R01": "Re-pull the barcode BOM price and correct the line price before invoicing.",
        "R02": "Recompute price_before_tax from price and the applied discount.",
        "R03": "Recalculate amount = price_before_tax + tax.",
        "R04": "Apply 3% GST; check the tax master for this SKU/location.",
        "R05": "Re-sum line items into header sub_total.",
        "R06": "Recompute grand_total = sub_total + tax.",
        "R07": "Reverse the excess discount or get the required approval.",
        "R09": "Validate the coupon config / attach the authorising coupon code.",
        "R10": "Trigger the invoice push to ERP before dispatch.",
        "R11": "Set a valid EDD on/after the order date.",
        "R12": "Reconcile the sub-inventory transfer (ITR) between ordered and fulfilled.",
        "R13": "Attach the diamond certificate before shipment.",
        "R14": "Link the order to a valid customer master record.",
        "R15": "Correct the shipping pincode / state.",
        "R16": "Obtain approval / capture the missing channel-required field.",
        "R17": "Block the line until barcode/SKU/price are populated.",
    }
    return table.get(f.rule_id, "Review and correct the flagged field.")


def explain(result: AuditResult) -> dict:
    """Return a narrative dict. Uses Claude if configured, else offline fallback."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return _offline(result)
    try:
        import anthropic
    except ImportError:
        return _offline(result)

    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    "Audit findings (already computed, do not change verdicts):\n"
                    + json.dumps(_payload(result), indent=2, default=str)
                    + '\n\nReturn JSON: {"verdict": str, "issues": [{"title","severity",'
                      '"rupee_impact","explanation","fix","line"}], "anomalies": [str]}'
                ),
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        data["engine"] = MODEL
        return data
    except Exception as exc:
        out = _offline(result)
        out["engine"] = f"offline-fallback (Claude error: {exc})"
        return out
