"""Quick sanity tests for the rule engine. Run: python -m pytest tests/  (or python tests/test_engine.py)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit.engine import audit


def check(order_id, expected_verdict, expect_rules=()):
    r = audit(order_id)
    assert r.verdict == expected_verdict, f"{order_id}: got {r.verdict}, want {expected_verdict}"
    fired = {f.rule_id for f in r.failures}
    for rid in expect_rules:
        assert rid in fired, f"{order_id}: expected rule {rid}, fired {fired}"
    print(f"OK {order_id}: {r.verdict} ₹{r.rupees_at_risk:,.2f} rules={sorted(fired)}")


def test_pass():
    check("CL-ORD-1001", "PASS")


def test_price_leak():
    check("CL-ORD-1002", "FLAG", ["R01"])


def test_discount_abuse():
    check("CL-ORD-1003", "FLAG", ["R07", "R09"])


def test_invoice_missing():
    check("CL-ORD-1004", "FLAG", ["R10", "R11"])


def test_itr_mismatch():
    check("CL-ORD-1005", "FLAG", ["R12", "R16"])


if __name__ == "__main__":
    for fn in [test_pass, test_price_leak, test_discount_abuse, test_invoice_missing, test_itr_mismatch]:
        fn()
    print("\nAll sanity tests passed.")
