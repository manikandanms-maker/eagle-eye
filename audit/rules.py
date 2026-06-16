"""The rule catalog — deterministic checks, the SOURCE OF TRUTH.

Each rule is a function (Order) -> list[Finding]. The LLM never runs these; it only
explains their output. Add a rule by writing a function and listing it in ALL_RULES.

Finding fields:
  module, rule_id, severity, status, title, expected, actual, rupee_impact, detail
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
import re

from . import config as C
from .schema import Order, Item, to_float, is_blank


@dataclass
class Finding:
    module: str
    rule_id: str
    title: str
    status: str = "PASS"            # PASS | FAIL
    severity: str = "INFO"          # CRITICAL | HIGH | MEDIUM | LOW | INFO
    expected: str = ""
    actual: str = ""
    rupee_impact: float = 0.0       # estimated ₹ at risk for this finding
    detail: str = ""
    line: Optional[str] = None      # which item (sku/barcode), if line-level

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def _money(x: Optional[float]) -> str:
    return "—" if x is None else f"₹{x:,.2f}"


def _close(a: Optional[float], b: Optional[float], tol: float = C.MONEY_TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


# =============================================================================
# PRICING module
# =============================================================================
def r01_barcode_price(order: Order) -> list[Finding]:
    """R01: item price must equal the barcode BOM computed price (pricing_engine)."""
    out = []
    for it in order.items:
        computed = to_float(it.pricing_reference.get("computed_price"))
        price = it.f("price")
        if computed is None or price is None:
            continue
        if not _close(price, computed):
            leak = computed - price  # underpriced => positive leakage
            out.append(Finding(
                module="PRICING", rule_id="R01",
                title="Barcode price mismatch vs BOM",
                status="FAIL",
                severity="CRITICAL" if abs(leak) >= 500 else "HIGH",
                expected=f"price == computed {_money(computed)}",
                actual=f"price {_money(price)}",
                rupee_impact=max(leak, 0.0),
                line=f"{it.sku} / {it.barcode}",
                detail=(f"Barcode {it.barcode} billed at {_money(price)} but BOM "
                        f"(metal+making+stone) computes {_money(computed)} — "
                        f"{'UNDERPRICED, revenue leakage' if leak > 0 else 'overpriced'}."),
            ))
    return out


def r02_discount_math(order: Order) -> list[Finding]:
    """R02: price_before_tax == price - (flat_discount or price*discount%)."""
    out = []
    for it in order.items:
        price = it.f("price")
        pbt = it.f("price_before_tax")
        if price is None or pbt is None:
            continue
        flat = it.f("flat_discount") or 0.0
        pct = it.f("discount_percent") or 0.0
        disc = flat if flat else price * pct / 100.0
        expected_pbt = price - disc
        if not _close(pbt, expected_pbt):
            gap = abs(pbt - expected_pbt)
            out.append(Finding(
                module="PRICING", rule_id="R02",
                title="Discount math does not tie out",
                status="FAIL", severity="HIGH",
                expected=f"price_before_tax == {_money(expected_pbt)}",
                actual=_money(pbt),
                rupee_impact=gap,
                line=f"{it.sku} / {it.barcode}",
                detail=f"price {_money(price)} − discount {_money(disc)} ≠ {_money(pbt)}.",
            ))
    return out


def r03_item_total(order: Order) -> list[Finding]:
    """R03: amount == price_before_tax + tax."""
    out = []
    for it in order.items:
        pbt = it.f("price_before_tax")
        tax = it.f("tax")
        amount = it.f("amount")
        if None in (pbt, tax, amount):
            continue
        if not _close(amount, pbt + tax):
            out.append(Finding(
                module="PRICING", rule_id="R03",
                title="Item total wrong",
                status="FAIL", severity="HIGH",
                expected=f"amount == {_money(pbt + tax)}",
                actual=_money(amount),
                rupee_impact=abs(amount - (pbt + tax)),
                line=f"{it.sku} / {it.barcode}",
                detail="amount must equal price_before_tax + tax.",
            ))
    return out


def r04_tax_rate(order: Order) -> list[Finding]:
    """R04: tax ~= price_before_tax * GST_RATE."""
    out = []
    for it in order.items:
        pbt = it.f("price_before_tax")
        tax = it.f("tax")
        if pbt is None or tax is None:
            continue
        expected_tax = pbt * C.GST_RATE
        # tolerate 1% of the tax value or the flat money tolerance, whichever larger
        tol = max(C.MONEY_TOLERANCE, expected_tax * 0.01)
        if abs(tax - expected_tax) > tol:
            out.append(Finding(
                module="PRICING", rule_id="R04",
                title=f"GST not at {C.GST_RATE*100:.0f}%",
                status="FAIL",
                severity="HIGH" if tax < expected_tax else "MEDIUM",
                expected=f"tax ≈ {_money(expected_tax)} ({C.GST_RATE*100:.0f}%)",
                actual=_money(tax),
                rupee_impact=max(expected_tax - tax, 0.0),
                line=f"{it.sku} / {it.barcode}",
                detail="Wrong/zero GST is both a compliance issue and revenue leakage.",
            ))
    return out


def r05_header_subtotal(order: Order) -> list[Finding]:
    """R05: header sub_total == sum of item price_before_tax."""
    pbts = [it.f("price_before_tax") for it in order.items]
    if not pbts or any(p is None for p in pbts):
        return []
    total = sum(pbts)
    header_sub = order.h("sub_total")
    if header_sub is None or _close(header_sub, total):
        return []
    return [Finding(
        module="PRICING", rule_id="R05",
        title="Header sub_total ≠ Σ line items",
        status="FAIL", severity="HIGH",
        expected=f"sub_total == {_money(total)}",
        actual=_money(header_sub),
        rupee_impact=abs(header_sub - total),
        detail="Header and line items disagree — billing will be wrong.",
    )]


def r06_grand_total(order: Order) -> list[Finding]:
    """R06: grand_total == sub_total + tax."""
    sub = order.h("sub_total")
    tax = order.h("tax")
    grand = order.h("grand_total")
    if None in (sub, tax, grand) or _close(grand, sub + tax):
        return []
    return [Finding(
        module="PRICING", rule_id="R06",
        title="Grand total wrong",
        status="FAIL", severity="HIGH",
        expected=f"grand_total == {_money(sub + tax)}",
        actual=_money(grand),
        rupee_impact=abs(grand - (sub + tax)),
        detail="grand_total must equal sub_total + tax.",
    )]


def r17_price_sanity(order: Order) -> list[Finding]:
    """R17: every line has a positive price and a well-formed barcode + sku."""
    out = []
    for it in order.items:
        price = it.f("price")
        if price is None or price <= 0:
            out.append(Finding(
                module="PRICING", rule_id="R17",
                title="Zero / missing price", status="FAIL", severity="CRITICAL",
                expected="price > 0", actual=_money(price),
                rupee_impact=0.0, line=f"{it.sku} / {it.barcode}",
                detail="A zero-priced shippable line is direct revenue leakage."))
        if is_blank(it.barcode) or is_blank(it.sku):
            out.append(Finding(
                module="PRICING", rule_id="R17",
                title="Missing barcode / SKU", status="FAIL", severity="HIGH",
                expected="barcode and sku present", actual=f"sku={it.sku} barcode={it.barcode}",
                line=f"{it.sku} / {it.barcode}",
                detail="Line cannot be priced/tracked without barcode + SKU."))
    return out


# =============================================================================
# DISCOUNT module
# =============================================================================
def r07_discount_cap(order: Order) -> list[Finding]:
    """R07: effective item discount % within the cap for this order type."""
    cap = C.DISCOUNT_CAP.get(order.order_type, C.DISCOUNT_CAP["DEFAULT"])
    out = []
    for it in order.items:
        price = it.f("price")
        pbt = it.f("price_before_tax")
        if not price or pbt is None:
            continue
        eff = (price - pbt) / price * 100.0
        if eff > cap + 0.01:
            out.append(Finding(
                module="DISCOUNT", rule_id="R07",
                title=f"Discount exceeds {order.order_type} cap",
                status="FAIL", severity="HIGH",
                expected=f"≤ {cap:.0f}%",
                actual=f"{eff:.1f}%",
                rupee_impact=(eff - cap) / 100.0 * price,
                line=f"{it.sku} / {it.barcode}",
                detail=f"Effective discount {eff:.1f}% exceeds {cap:.0f}% cap for "
                       f"{order.order_type} — unauthorised discount / revenue leakage.",
            ))
    return out


# =============================================================================
# COUPON module
# =============================================================================
def r09_coupon_consistency(order: Order) -> list[Finding]:
    """R09: coupon code <-> discount value must agree (header level)."""
    code = order.header.get("coupon_code")
    disc = order.h("discount_amount") or 0.0
    has_code = not is_blank(code)
    out = []
    if has_code and disc <= 0:
        out.append(Finding(
            module="COUPON", rule_id="R09",
            title="Coupon applied but ₹0 value",
            status="FAIL", severity="MEDIUM",
            expected="discount_amount > 0", actual=_money(disc),
            detail=f"Coupon '{code}' present but produced no discount — "
                   f"misconfigured coupon (customer-experience + reporting risk).",
        ))
    if not has_code and disc > 0:
        out.append(Finding(
            module="COUPON", rule_id="R09",
            title="Discount without a coupon code",
            status="FAIL", severity="HIGH",
            expected="coupon_code present for header discount", actual="(blank)",
            rupee_impact=disc,
            detail=f"{_money(disc)} discount applied with no coupon code — "
                   f"unauthorised discount / revenue leakage.",
        ))
    return out


# =============================================================================
# INVOICE module
# =============================================================================
def r10_invoice_push(order: Order) -> list[Finding]:
    """R10: shippable items must carry an invoice number (invoice push to ERP)."""
    out = []
    for it in order.items:
        status = (it.fulfillment.get("status") or it.detail.get("status") or "").strip()
        invoice = it.fulfillment.get("invoice_no")
        if status in C.SHIPPABLE_STATUSES and is_blank(invoice):
            out.append(Finding(
                module="INVOICE", rule_id="R10",
                title="Invoice push missing",
                status="FAIL", severity="CRITICAL",
                expected="invoice_no present", actual="(blank)",
                rupee_impact=it.f("amount") or 0.0,
                line=f"{it.sku} / {it.barcode}",
                detail=f"Item is '{status}' but has no invoice_no — it shipped without "
                       f"an invoice pushed to ERP. Unbillable revenue leakage.",
            ))
    return out


# =============================================================================
# DELIVERY module
# =============================================================================
def r11_delivery_dates(order: Order) -> list[Finding]:
    """R11: EDD present and not before the order date."""
    out = []
    order_date = (order.header.get("order_date") or "")[:10]
    for it in order.items:
        edd = (it.detail.get("edd_date") or "")
        if is_blank(edd):
            out.append(Finding(
                module="DELIVERY", rule_id="R11",
                title="Missing delivery date (EDD)",
                status="FAIL", severity="MEDIUM",
                expected="edd_date present", actual="(blank)",
                line=f"{it.sku} / {it.barcode}",
                detail="No estimated delivery date — SLA/customer-promise risk."))
            continue
        if order_date and edd[:10] < order_date:
            out.append(Finding(
                module="DELIVERY", rule_id="R11",
                title="Delivery date before order date",
                status="FAIL", severity="HIGH",
                expected=f"edd_date >= order_date ({order_date})",
                actual=edd[:10],
                line=f"{it.sku} / {it.barcode}",
                detail="EDD precedes the order date — impossible / data error."))
    return out


# =============================================================================
# FULFILLMENT / ITR module  (sub-inventory transfer / sync consistency)
# =============================================================================
def r12_detail_vs_fulfillment(order: Order) -> list[Finding]:
    """R12: ORDER DETAIL vs FULFILLMENT DETAIL must agree on barcode & money."""
    out = []
    for it in order.items:
        if not it.fulfillment:
            continue
        # barcode/sku identity
        if not is_blank(it.detail.get("barcode")) and not is_blank(it.fulfillment.get("barcode")):
            if str(it.detail.get("barcode")) != str(it.fulfillment.get("barcode")):
                out.append(Finding(
                    module="ITR", rule_id="R12",
                    title="Barcode mismatch (ordered vs fulfilled)",
                    status="FAIL", severity="CRITICAL",
                    expected=f"ordered {it.detail.get('barcode')}",
                    actual=f"fulfilled {it.fulfillment.get('barcode')}",
                    line=f"{it.sku}",
                    detail="A different barcode was fulfilled than ordered — "
                           "sub-inventory transfer / ITR mismatch."))
        # money identity across the two views
        for key, sev in (("amount", "HIGH"), ("price", "HIGH")):
            d, fdv = it.f(key), it.ff(key)
            if d is not None and fdv is not None and not _close(d, fdv):
                out.append(Finding(
                    module="ITR", rule_id="R12",
                    title=f"{key} mismatch ordered vs fulfilled",
                    status="FAIL", severity=sev,
                    expected=f"ordered {_money(d)}", actual=f"fulfilled {_money(fdv)}",
                    rupee_impact=abs(d - fdv), line=f"{it.sku} / {it.barcode}",
                    detail=f"{key} differs between ORDER DETAIL and FULFILLMENT DETAIL — "
                           f"transfer/sync did not reconcile."))
    return out


def r13_diamond_cert(order: Order) -> list[Finding]:
    """R13: diamond items must carry a certificate number."""
    out = []
    for it in order.items:
        if it.is_diamond:
            cert = it.fulfillment.get("certificate_no") or it.detail.get("certificate_no")
            if is_blank(cert):
                out.append(Finding(
                    module="ITEM", rule_id="R13",
                    title="Diamond item missing certificate",
                    status="FAIL", severity="HIGH",
                    expected="certificate_no present", actual="(blank)",
                    line=f"{it.sku} / {it.barcode}",
                    detail="Diamond/solitaire shipped without a certificate — "
                           "compliance + customer-trust risk."))
    return out


# =============================================================================
# CUSTOMER + ADDRESS modules
# =============================================================================
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def r14_customer(order: Order) -> list[Finding]:
    out = []
    h = order.header
    if is_blank(h.get("customer_id")) or is_blank(h.get("customer_name")):
        out.append(Finding(
            module="CUSTOMER", rule_id="R14",
            title="Missing customer identity",
            status="FAIL", severity="MEDIUM",
            expected="customer_id and customer_name present",
            actual=f"id={h.get('customer_id')} name={h.get('customer_name')}",
            detail="Order not tied to a customer master record."))
    email = h.get("email")
    if not is_blank(email) and not _EMAIL_RE.match(str(email).strip()):
        out.append(Finding(
            module="CUSTOMER", rule_id="R14",
            title="Malformed email",
            status="FAIL", severity="LOW",
            expected="valid email", actual=str(email),
            detail="Invoice / comms will bounce."))
    return out


def r15_address(order: Order) -> list[Finding]:
    out = []
    h = order.header
    pin = str(h.get("ship_pin_code") or h.get("bill_pin_code") or "").strip()
    if is_blank(pin) or not re.fullmatch(r"\d{6}", pin):
        out.append(Finding(
            module="ADDRESS", rule_id="R15",
            title="Invalid / missing pincode",
            status="FAIL", severity="MEDIUM",
            expected="6-digit pincode", actual=pin or "(blank)",
            detail="Undeliverable address + wrong tax jurisdiction risk."))
    if is_blank(h.get("ship_state") or h.get("bill_state")):
        out.append(Finding(
            module="ADDRESS", rule_id="R15",
            title="Missing state",
            status="FAIL", severity="MEDIUM",
            expected="state present", actual="(blank)",
            detail="State drives GST (CGST/SGST vs IGST) — tax risk."))
    return out


# =============================================================================
# RESTRICTION module (order-type-specific policy)
# =============================================================================
def r16_restrictions(order: Order) -> list[Finding]:
    out = []
    h = order.header
    grand = order.h("grand_total") or order.h("order_amount") or 0.0

    # high value needs financial approval
    if grand >= C.FINANCIAL_APPROVAL_THRESHOLD and str(h.get("financial_approval", "")).upper() != "YES":
        out.append(Finding(
            module="RESTRICTION", rule_id="R16",
            title="High-value order without financial approval",
            status="FAIL", severity="HIGH",
            expected=f"financial_approval=YES (>= {_money(C.FINANCIAL_APPROVAL_THRESHOLD)})",
            actual=str(h.get("financial_approval") or "(blank)"),
            rupee_impact=0.0,
            detail="Policy bypass: large order released without approval."))

    if order.order_type == "ONLINE" and is_blank(h.get("payment_mode")) and is_blank(h.get("payment_source")):
        out.append(Finding(
            module="RESTRICTION", rule_id="R16",
            title="Online order missing payment mode",
            status="FAIL", severity="HIGH",
            expected="payment_mode/payment_source present for ONLINE",
            actual="(blank)",
            detail="Online order with no captured payment — fulfilment-before-payment risk."))

    if order.order_type == "OLDGOLD":
        og = to_float(h.get("old_gold_value")) or to_float(h.get("oldgold_value"))
        if og is None or og <= 0:
            out.append(Finding(
                module="RESTRICTION", rule_id="R16",
                title="Old-gold order missing exchange value",
                status="FAIL", severity="HIGH",
                expected="old_gold_value > 0",
                actual=_money(og),
                detail="Old-gold exchange with no recorded buy-back value — "
                       "metal accounting / revenue leakage."))
    return out


# -----------------------------------------------------------------------------
ALL_RULES: list[Callable[[Order], list[Finding]]] = [
    r01_barcode_price, r02_discount_math, r03_item_total, r04_tax_rate,
    r05_header_subtotal, r06_grand_total, r17_price_sanity,
    r07_discount_cap, r09_coupon_consistency,
    r10_invoice_push, r11_delivery_dates,
    r12_detail_vs_fulfillment, r13_diamond_cert,
    r14_customer, r15_address, r16_restrictions,
]
