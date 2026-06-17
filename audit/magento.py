"""Magento order lookup + first-check validation.

Uses the CaratLane sales_flat_order join graph. The base query mirrors the
hackathon checklist; validate_order() applies each criterion explicitly so
the UI can show pass/fail per check even when the order exists but is not
audit-eligible yet.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from .db import DatabaseConfigError, db_configured, fetch_all, fetch_one

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── SQL ──────────────────────────────────────────────────────────────────────

ORDER_HEADER_SELECT = """
SELECT
  sfo.entity_id,
  --sfo.increment_id,
  sfo.hash_id,
  esfo.order_id AS erp_order_id,
  sfo.is_fusion_order,
  sfo.is_pushed,
  sfo.customer_email,
  sfo.customer_firstname,
  sfo.customer_lastname,
  sfo.fusion_party_number,
  sfo.net_payable,
  sfo.source,
  sfo.created_at
"""

ORDER_LOOKUP_BY_INCREMENT = ORDER_HEADER_SELECT + """
FROM sales_flat_order sfo
LEFT JOIN erp_sales_flat_order esfo ON esfo.order_id = sfo.entity_id
WHERE sfo.increment_id = %(order_number)s
LIMIT 1
"""

ORDER_LOOKUP_BY_ENTITY = ORDER_HEADER_SELECT + """
FROM sales_flat_order sfo
LEFT JOIN erp_sales_flat_order esfo ON esfo.order_id = sfo.entity_id
WHERE sfo.entity_id = %(entity_id)s
LIMIT 1
"""

ORDER_LOOKUP_BY_HASH = ORDER_HEADER_SELECT + """
FROM sales_flat_order sfo
LEFT JOIN erp_sales_flat_order esfo ON esfo.order_id = sfo.entity_id
WHERE sfo.hash_id = %(order_number)s
LIMIT 1
"""

ORDER_LOOKUP_BY_ERP = ORDER_HEADER_SELECT + """
FROM erp_sales_flat_order esfo
JOIN sales_flat_order sfo ON sfo.entity_id = esfo.order_id
WHERE esfo.order_id = %(entity_id)s
LIMIT 1
"""

ORDER_LINES_SQL = """
SELECT
  elmd.location_name,
  esfo.order_id,
  sfo.entity_id,
  sfo.hash_id,
  sfo.net_payable,
  sfoi.sku_size AS item_number,
  --sfoi.sku_size,
  sfoi.name,
  sfoi.order_processing_type,
  sfoiq.barcode,
  sfoiq.price_before_tax,
  sfoiq.tax,
  sfoiq.final_price,
  sfoiq.tax_breakup,
  sfo.fusion_party_number,
  sfo.customer_firstname,
  sfo.customer_lastname,
  sfoa.email AS shipping_email,
  sfo.billing_address_id,
  sfoi.invoice_mode,
  sfo.source,
  elmd.pan_no,
  elmd.gstin_no,
  sfoiq.barcode_source_location,
  sfoiqai.barcode_reservation_id,
  sfoiqai.barcode_reserved_in_fusion,
  sfoi.price_breakup,
  sfoi.expected_delivery_date_min,
  sfoi.expected_delivery_date_max,
  sfoi.erp_doc_no,
  sfoi.is_pushed AS item_is_pushed,
  sfoi.is_payment_updated,
  sfoi.financial_approval,
  sfoi.order_type AS item_order_type,
  sfoi.discount_breakups,
  sfoiq.erp_status,
  sfoiqai.manual_dispatch_status,
  sfoiqai.final_delivery_date,
  sfoiqai.pick_status,
  sfoa.fusion_address_id,
  sfoi.item_id,
  sfoiq.id AS qty_id
FROM sales_flat_order sfo
JOIN sales_flat_order_item sfoi ON sfoi.order_id = sfo.entity_id
JOIN sales_flat_order_item_qty sfoiq ON sfoiq.item_id = sfoi.item_id
JOIN sales_flat_order_item_qty_additional_infos sfoiqai ON sfoiqai.qty_id = sfoiq.id
LEFT JOIN sales_flat_order_address sfoa
  ON sfoa.parent_id = sfo.entity_id AND sfoa.address_type = 'shipping'
LEFT JOIN erp_sales_flat_order esfo ON esfo.order_id = sfo.entity_id
LEFT JOIN erp_location_master_dtl elmd ON elmd.location_id = sfoiq.shipping_location_id
WHERE sfo.entity_id = %(entity_id)s
  AND TRIM(COALESCE(sfoiq.barcode, '')) <> ''
ORDER BY sfo.net_payable DESC, sfoi.item_id, sfoiq.id
"""

# caratlane_invoices is ~1.8M rows and ijq_id is NOT indexed, so the old derived-table
# join JSON-parsed every row (~5s). It DOES have an index on order_id, so we filter by
# ci.order_id = sfo.entity_id — the JSON_EXTRACT barcode transform then runs on only this
# order's few invoices (~50ms).
INVOICE_BARCODE_EXPR = (
    "REPLACE(REPLACE(SUBSTRING_INDEX(SUBSTRING_INDEX("
    "JSON_UNQUOTE(JSON_EXTRACT(ci.meta, '$.barcodeInfo')), '=>', 1), '{', -1), '\"', ''), '\\\\', '')"
)
INVOICE_BY_ORDER_SQL = f"""
SELECT
  sfoiq.barcode,
  sfoiq.erp_status,
  ci.invoice_number,
  {INVOICE_BARCODE_EXPR} AS invoice_barcode,
  CASE WHEN ci.invoice_number IS NOT NULL THEN 'INVOICED' ELSE 'PENDING_INVOICE' END AS invoice_flag
FROM sales_flat_order sfo
JOIN sales_flat_order_item sfoi ON sfo.entity_id = sfoi.order_id
JOIN sales_flat_order_item_qty sfoiq ON sfoiq.item_id = sfoi.item_id
LEFT JOIN caratlane_invoices ci
       ON ci.order_id = sfo.entity_id
      AND {INVOICE_BARCODE_EXPR} = sfoiq.barcode
WHERE sfo.entity_id = %(entity_id)s
  AND TRIM(COALESCE(sfoiq.barcode, '')) <> ''
ORDER BY sfoiq.barcode
"""

VENDOR_QC_BY_PO_SQL = """
SELECT
  ipoiq.barcode,
  ipoiq.po_number,
  k.status_name,
  JSON_UNQUOTE(
    JSON_EXTRACT(
      ipoiq.status_update_time_stamp,
      CONCAT('$.', JSON_QUOTE(k.status_name))
    )
  ) AS status_time
FROM indus_purchase_orders_item_qty ipoiq
CROSS JOIN JSON_TABLE(
  JSON_KEYS(ipoiq.status_update_time_stamp),
  '$[*]' COLUMNS (status_name VARCHAR(100) PATH '$')
) k
WHERE ipoiq.po_number IN ({placeholders})
ORDER BY STR_TO_DATE(
  JSON_UNQUOTE(
    JSON_EXTRACT(
      ipoiq.status_update_time_stamp,
      CONCAT('$.', JSON_QUOTE(k.status_name))
    )
  ),
  '%%Y-%%m-%%d %%H:%%i:%%s'
) DESC
"""

BARCODE_ATTR_BY_BARCODES_SQL = """
SELECT
  TRIM(eba.stock_code) AS stock_code,
  elmd.location_name,
  eba.transaction_type,
  eba.barcode_status,
  eba.transaction_status,
  eba.updated_at
FROM erp_barcode_attributes eba
LEFT JOIN erp_location_master_dtl elmd
  ON elmd.location_code = eba.location_code
-- Match the raw indexed column (params are already trimmed). Wrapping the column in
-- TRIM(...) disabled index_erp_barcode_attributes_on_stock_code and forced a full scan (~1.7s).
WHERE eba.stock_code IN ({placeholders})
"""

ELIGIBLE_ERP_STATUSES = frozenset({"On Hold", "Processing", "Dispatched"})


@dataclass
class CheckResult:
    name: str
    label: str
    module: str
    passed: bool
    expected: str
    actual: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "module": self.module,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass
class ValidationResult:
    order_number: str
    found: bool
    valid: bool
    order_id: Optional[str] = None
    hash_id: Optional[str] = None
    entity_id: Optional[int] = None
    checks: list[CheckResult] = field(default_factory=list)
    customer: dict = field(default_factory=dict)
    header: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    line_count: int = 0
    message: str = ""
    meta: str = ""
    integrations: dict = field(default_factory=dict)
    work_orders: list[dict] = field(default_factory=list)
    source: str = "magento"

    def to_dict(self) -> dict:
        return {
            "order_number": self.order_number,
            "found": self.found,
            "valid": self.valid,
            "order_id": self.order_id,
            "hash_id": self.hash_id,
            "entity_id": self.entity_id,
            "checks": [c.to_dict() for c in self.checks],
            "customer": _json_safe(self.customer),
            "header": _json_safe(self.header),
            "lines": [_json_safe(row) for row in self.lines],
            "line_count": self.line_count,
            "message": self.message,
            "meta": self.meta,
            "integrations": _json_safe(self.integrations),
            "work_orders": [_json_safe(row) for row in self.work_orders],
            "source": self.source,
        }


def _json_safe(value: Any) -> Any:
    """Make DB values JSON-serializable."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _str_val(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_truthy_flag(value: Any) -> bool:
    s = _str_val(value).lower()
    return s in {"1", "true", "yes", "y"}


def _customer_name(header: dict, lines: list[dict]) -> str:
    first = _str_val(header.get("customer_firstname"))
    last = _str_val(header.get("customer_lastname"))
    if first or last:
        return f"{first} {last}".strip()
    if lines:
        first = _str_val(lines[0].get("customer_firstname"))
        last = _str_val(lines[0].get("customer_lastname"))
        if first or last:
            return f"{first} {last}".strip()
    return ""


def _customer_email(header: dict, lines: list[dict]) -> str:
    for candidate in (
        header.get("customer_email"),
        lines[0].get("shipping_email") if lines else None,
    ):
        email = _str_val(candidate)
        if email:
            return email
    return ""


def _extract_customer(header: dict, lines: list[dict], searched_as: str) -> dict:
    pan_values = sorted({
        _str_val(ln.get("pan_no")) for ln in lines if _str_val(ln.get("pan_no"))
    })
    return {
        "searched_as": searched_as,
        "name": _customer_name(header, lines),
        "email": _customer_email(header, lines),
        "fusion_party_number": _str_val(header.get("fusion_party_number")
                                         or (lines[0].get("fusion_party_number") if lines else "")),
        "pan_no": pan_values[0] if len(pan_values) == 1 else (", ".join(pan_values) if pan_values else ""),
        "hash_id": _str_val(header.get("hash_id")),
        "entity_id": header.get("entity_id"),
        "source": _str_val(header.get("source")),
    }


def _order_meta_line(header: dict) -> str:
    parts = []
    hsh = _str_val(header.get("hash_id"))
    eid = header.get("entity_id")
    if hsh:
        parts.append(f"hash: {hsh}")
    if eid is not None:
        parts.append(f"entity_id: {eid}")
    return " · ".join(parts)


# ── Pre-audit rule catalog (first-check gates) ───────────────────────────────

def _check_fusion_order(header: dict, lines: list[dict]) -> CheckResult:
    fusion = header.get("is_fusion_order")
    return CheckResult(
        name="is_fusion_order", label="Fusion order", module="ORDER",
        passed=_is_truthy_flag(fusion),
        expected="1 (Fusion order)", actual=_str_val(fusion) or "empty",
        detail="Order must be flagged as a Fusion order.",
    )


def _check_is_pushed(header: dict, lines: list[dict]) -> CheckResult:
    pushed = header.get("is_pushed")
    return CheckResult(
        name="is_pushed", label="Pushed to ERP", module="ORDER",
        passed=_is_truthy_flag(pushed),
        expected="1 (pushed)", actual=_str_val(pushed) or "empty",
        detail="Order header must be pushed to ERP.",
    )


def _check_non_test_email(header: dict, lines: list[dict]) -> CheckResult:
    email = _customer_email(header, lines).lower()
    not_test = "test" not in email if email else False
    return CheckResult(
        name="non_test_email", label="Non-test customer email", module="ORDER",
        passed=not_test and bool(email),
        expected="email not containing 'test'", actual=email or "empty",
        detail="Test customer emails are excluded from audit scope.",
    )


def _check_barcode_present(header: dict, lines: list[dict]) -> CheckResult:
    barcoded = [ln for ln in lines if _str_val(ln.get("barcode"))]
    return CheckResult(
        name="barcode_present", label="Barcode on line items", module="ORDER",
        passed=len(barcoded) > 0 and len(barcoded) == len(lines),
        expected="non-empty barcode on every qty line",
        actual=f"{len(barcoded)}/{len(lines)} lines with barcode",
        detail="Each order item qty must carry a barcode for pricing reconciliation.",
    )


def _check_erp_status(header: dict, lines: list[dict]) -> CheckResult:
    eligible = [ln for ln in lines if _str_val(ln.get("erp_status")) in ELIGIBLE_ERP_STATUSES]
    status_summary = ", ".join(
        sorted({_str_val(ln.get("erp_status")) or "empty" for ln in lines})
    ) or "none"
    return CheckResult(
        name="erp_status", label="ERP status eligible", module="ORDER",
        passed=len(eligible) > 0 and len(eligible) == len(lines),
        expected=f"erp_status in {sorted(ELIGIBLE_ERP_STATUSES)}",
        actual=status_summary,
        detail="Only On Hold / Processing / Dispatched lines are in the first-check scope.",
    )


def _check_customer_name(header: dict, lines: list[dict]) -> CheckResult:
    name = _customer_name(header, lines)
    return CheckResult(
        name="customer_name", label="Customer name present", module="CUSTOMER",
        passed=bool(name),
        expected="non-empty customer name", actual=name or "empty",
        detail="Order must be tied to a customer name for invoice and comms.",
    )


def _check_customer_email(header: dict, lines: list[dict]) -> CheckResult:
    email = _customer_email(header, lines)
    valid = bool(email) and bool(_EMAIL_RE.match(email))
    return CheckResult(
        name="customer_email", label="Customer email valid", module="CUSTOMER",
        passed=valid,
        expected="valid email address", actual=email or "empty",
        detail="A well-formed email is required for invoice delivery and notifications.",
    )


def _norm_token(value: Any) -> str:
    return re.sub(r"\s+", " ", _str_val(value).upper())


def _values_match(a: Any, b: Any) -> bool:
    left, right = _norm_token(a), _norm_token(b)
    if not left or not right:
        return True
    return left == right or left in right or right in left


_TXN_TYPE_ALIASES = {
    "SOI": "SALES ORDER ISSUE",
    "POR": "PURCHASE ORDER RECEIPT",
    "TOI": "TRANSFER ORDER ISSUE",
    "TOR": "TRANSFER ORDER RECEIPT",
}


def _txn_types_align(a: Any, b: Any) -> bool:
    left, right = _norm_token(a), _norm_token(b)
    if not left or not right:
        return True
    if _values_match(left, right):
        return True
    left_full = _TXN_TYPE_ALIASES.get(left, left)
    right_full = _TXN_TYPE_ALIASES.get(right, right)
    return _values_match(left_full, right_full)


def _check_expected_delivery_dates(header: dict, lines: list[dict]) -> CheckResult:
    missing = [
        _str_val(ln.get("barcode"))
        for ln in lines
        if not _str_val(ln.get("expected_delivery_date_min"))
        or not _str_val(ln.get("expected_delivery_date_max"))
    ]
    missing = [bc for bc in missing if bc]
    return CheckResult(
        name="expected_delivery_dates",
        label="Expected delivery dates",
        module="ORDER",
        passed=len(missing) == 0,
        expected="expected_delivery_date_min and max on every line",
        actual=f"{len(lines) - len(missing)}/{len(lines)} lines complete"
        if lines else "no lines",
        detail="Both EDD min and max must be present for each barcode line.",
    )


def _check_fusion_party(header: dict, lines: list[dict]) -> CheckResult:
    party = _str_val(header.get("fusion_party_number")
                     or (lines[0].get("fusion_party_number") if lines else ""))
    return CheckResult(
        name="fusion_party_number", label="Fusion party number", module="CUSTOMER",
        passed=bool(party),
        expected="non-empty fusion_party_number", actual=party or "empty",
        detail="Customer must be linked to an ERP Fusion party for billing sync.",
    )


def _check_barcode_location_sync(header: dict, lines: list[dict]) -> CheckResult:
    mismatches = [
        _str_val(ln.get("barcode"))
        for ln in lines
        if ln.get("location_mismatch")
    ]
    mismatches = [bc for bc in mismatches if bc]
    return CheckResult(
        name="barcode_location_sync",
        label="Barcode location (CL vs PaaS vs SaaS)",
        module="BARCODE",
        passed=len(mismatches) == 0,
        expected="CL, PaaS, and SaaS locations aligned",
        actual=", ".join(mismatches) if mismatches else "all aligned",
        detail="Compares erp_barcode_attributes, Fusion trx/loc, and SaaS location report.",
    )


def _check_barcode_transaction_sync(header: dict, lines: list[dict]) -> CheckResult:
    mismatches = [
        _str_val(ln.get("barcode"))
        for ln in lines
        if ln.get("transaction_mismatch")
    ]
    mismatches = [bc for bc in mismatches if bc]
    return CheckResult(
        name="barcode_transaction_sync",
        label="Barcode transaction (CL vs PaaS vs SaaS)",
        module="BARCODE",
        passed=len(mismatches) == 0,
        expected="transaction type/status aligned across systems",
        actual=", ".join(mismatches) if mismatches else "all aligned",
        detail="Compares erp_barcode_attributes, Fusion trx/loc, and SaaS transaction report.",
    )


def _check_no_duplicate_onhand(header: dict, lines: list[dict]) -> CheckResult:
    dupes = [
        f"{_str_val(ln.get('barcode'))} ({ln.get('duplicate_onhand_count')})"
        for ln in lines
        if ln.get("duplicate_onhand_count")
    ]
    return CheckResult(
        name="no_duplicate_onhand",
        label="No duplicate on-hand (PaaS)",
        module="BARCODE",
        passed=len(dupes) == 0,
        expected="no FG duplicate lot on-hand rows",
        actual=", ".join(dupes) if dupes else "none",
        detail="Fusion XXCL_INV_ONHAND_QUANTITIES_DETAIL duplicate lot_number check.",
    )


def _check_sold_not_onhand(header: dict, lines: list[dict]) -> CheckResult:
    sold = [_str_val(ln.get("barcode")) for ln in lines if ln.get("sold_onhand_present")]
    sold = [bc for bc in sold if bc]
    return CheckResult(
        name="sold_not_onhand",
        label="Sold barcodes not on-hand (PaaS)",
        module="BARCODE",
        passed=len(sold) == 0,
        expected="SOLD barcodes should not have on-hand qty",
        actual=", ".join(sold) if sold else "none",
        detail="Fusion sold trx with remaining on-hand quantity.",
    )


# ── Barcode price-breakup validation ────────────────────────────────────────
# Ported from pricing_engine prevalidators.go (BlockValidateStoreBarcodeDetail) and
# stage-branch PRC rules. A corrupted/incorrect price breakup on a barcode breaks
# order sync and leaks revenue, so we validate sfoi.price_breakup directly.
_PB_COMPONENT_VS_SUBTOTAL_BUFFER = 10.0    # Σ component FV vs sub_total (±₹10)
_PB_SUBTOTAL_TAX_VS_SELLING_BUFFER = 3.0   # sub_total + tax vs selling (±₹3)
_PB_TAX_RATE = 3.0                          # GST %
_PB_DIAMOND_MIN_RATE = 50000.0             # diamond rate floor (per-carat)


def _pb_components(price_breakup: Any) -> list[dict]:
    """Normalize sfoi.price_breakup (JSON str | list | dict-of-lists) -> flat components."""
    pb = price_breakup
    if isinstance(pb, (str, bytes)):
        text = str(pb).strip()
        if not text or text.upper() == "NULL":
            return []
        try:
            pb = json.loads(text)
        except (ValueError, TypeError):
            return []
    if isinstance(pb, dict):
        out: list[dict] = []
        for group in pb.values():
            if isinstance(group, list):
                out.extend(c for c in group if isinstance(c, dict))
        return out
    if isinstance(pb, list):
        return [c for c in pb if isinstance(c, dict)]
    return []


def _validate_line_price_breakup(barcode: str, price_breakup: Any) -> list[str]:
    """Apply pricing_engine barcode rules + PRC reconciliation. Returns error strings."""
    comps = _pb_components(price_breakup)
    if not comps:
        return []  # nothing to validate (barcode presence handled elsewhere)

    sub_total = tax_fv = selling = None
    component_sum = 0.0
    errors: list[str] = []

    for c in comps:
        label = str(c.get("l") or "").strip().lower()
        fv = _num(c.get("fv"))
        if label in ("sub_total", "subtotal"):
            sub_total = fv
            continue
        if label == "tax":
            tax_fv = fv
            continue
        if "selling" in label:
            selling = fv
            continue
        if label.endswith("_total"):
            continue  # intermediate summary (gold_total, diamond_total, component_total)
        # real component row
        if fv is not None:
            component_sum += fv
        w, r = _num(c.get("w")) or 0.0, _num(c.get("r")) or 0.0
        if "diamond" in label or "solitaire" in label:
            if w > 0 and not (fv or 0):
                errors.append(f"{barcode}: diamond priced 0 with weight {w:g}")
            elif r and r < _PB_DIAMOND_MIN_RATE:
                errors.append(f"{barcode}: diamond rate {r:.0f} < {_PB_DIAMOND_MIN_RATE:.0f}")
        elif "gemstone" in label:
            if w > 0 and not (fv or 0):
                errors.append(f"{barcode}: gemstone priced 0 with weight {w:g}")

    if sub_total is not None and abs(component_sum - sub_total) > _PB_COMPONENT_VS_SUBTOTAL_BUFFER:
        errors.append(f"{barcode}: Σ components {component_sum:.0f} ≠ sub_total {sub_total:.0f} (±10)")
    if sub_total is not None and selling is not None and \
            abs((sub_total + (tax_fv or 0)) - selling) > _PB_SUBTOTAL_TAX_VS_SELLING_BUFFER:
        errors.append(f"{barcode}: sub_total+tax {sub_total + (tax_fv or 0):.0f} ≠ selling {selling:.0f} (±3)")
    # Order price_breakup carries the tax row's `dp` as discount% (0), not the rate, so we
    # verify the rate by ratio: tax FV must be ~3% of sub_total (pricing_engine hasValidTax==3%).
    if sub_total and tax_fv is not None:
        expected = sub_total * _PB_TAX_RATE / 100.0
        if abs(tax_fv - expected) > max(2.0, expected * 0.01):
            errors.append(f"{barcode}: tax {tax_fv:.0f} ≠ 3% of sub_total ({expected:.0f})")
    return errors


def _check_price_breakup(header: dict, lines: list[dict]) -> CheckResult:
    """Barcode price-breakup validity (pricing_engine BlockValidateStoreBarcodeDetail + PRC)."""
    errors: list[str] = []
    for ln in lines:
        bc = _str_val(ln.get("barcode")) or _str_val(ln.get("item_number")) or "?"
        errors.extend(_validate_line_price_breakup(bc, ln.get("price_breakup")))
    shown = "; ".join(errors[:6]) + (f" (+{len(errors) - 6} more)" if len(errors) > 6 else "")
    return CheckResult(
        name="price_breakup_valid",
        label="Barcode price breakup",
        module="PRICING",
        passed=len(errors) == 0,
        expected="Σ components = sub_total (±10), sub_total+tax = selling (±3), tax 3%, diamond/gemstone priced",
        actual=shown if errors else "price breakup reconciles",
        detail="Validates sfoi.price_breakup against pricing_engine BlockValidateStoreBarcodeDetail "
               "(component FV vs sub_total, sub_total+tax vs selling, 3% tax, diamond rate/FV, gemstone FV).",
    )


PRE_AUDIT_RULES: list[Callable[[dict, list[dict]], CheckResult]] = [
    _check_fusion_order,
    _check_is_pushed,
    _check_non_test_email,
    _check_barcode_present,
    _check_erp_status,
    _check_expected_delivery_dates,
    _check_customer_name,
    _check_customer_email,
    _check_fusion_party,
    _check_barcode_location_sync,
    _check_barcode_transaction_sync,
    _check_no_duplicate_onhand,
    _check_sold_not_onhand,
    _check_price_breakup,
]


def _run_pre_audit_checks(header: dict, lines: list[dict]) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for rule in PRE_AUDIT_RULES:
        try:
            checks.append(rule(header, lines))
        except Exception as exc:
            checks.append(CheckResult(
                name=getattr(rule, "__name__", "rule"),
                label="Rule error",
                module="ENGINE",
                passed=False,
                expected="rule completes",
                actual=str(exc),
                detail="Pre-audit rule raised an exception.",
            ))
    return checks


def _fetch_invoice_map(entity_id: int) -> dict[str, dict]:
    """Barcode -> invoice fields (best row per barcode, prefer INVOICED)."""
    rows = fetch_all(INVOICE_BY_ORDER_SQL, {"entity_id": entity_id})
    by_barcode: dict[str, dict] = {}
    for row in rows:
        bc = _str_val(row.get("barcode"))
        if not bc:
            continue
        inv_num = row.get("invoice_number")
        entry = {
            "invoice_number": inv_num,
            "invoice_barcode": row.get("invoice_barcode"),
            "invoice_flag": row.get("invoice_flag"),
        }
        existing = by_barcode.get(bc)
        if not existing or (inv_num and not existing.get("invoice_number")):
            by_barcode[bc] = entry
    return by_barcode


def _fetch_vendor_qc_by_pos(po_numbers: list[str]) -> dict[tuple[str, str], dict]:
    """Latest Vendor QC status per (barcode, po_number) from Magento."""
    cleaned = sorted({_str_val(p) for p in po_numbers if _str_val(p)})
    if not cleaned or not db_configured():
        return {}

    placeholders = ", ".join(f"%(po{i})s" for i in range(len(cleaned)))
    params = {f"po{i}": v for i, v in enumerate(cleaned)}
    sql = VENDOR_QC_BY_PO_SQL.format(placeholders=placeholders)

    out: dict[tuple[str, str], dict] = {}
    try:
        rows = fetch_all(sql, params)
        for row in rows:
            bc = _str_val(row.get("barcode"))
            po = _str_val(row.get("po_number"))
            if not bc or not po:
                continue
            key = (bc, po)
            if key not in out:
                out[key] = {
                    "vendor_qc_status": row.get("status_name"),
                    "vendor_qc_status_time": row.get("status_time"),
                }
    except Exception:
        return out
    return out


def _fetch_cl_barcode_attrs(barcodes: list[str]) -> dict[str, dict]:
    """CL erp_barcode_attributes + location for order barcodes."""
    cleaned = sorted({_str_val(b) for b in barcodes if _str_val(b)})
    if not cleaned or not db_configured():
        return {}

    placeholders = ", ".join(f"%(bc{i})s" for i in range(len(cleaned)))
    params = {f"bc{i}": v for i, v in enumerate(cleaned)}
    sql = BARCODE_ATTR_BY_BARCODES_SQL.format(placeholders=placeholders)

    out: dict[str, dict] = {}
    try:
        rows = fetch_all(sql, params)
        for row in rows:
            bc = _str_val(row.get("stock_code"))
            if not bc:
                continue
            out[bc] = {
                "cl_location_name": row.get("location_name"),
                "cl_transaction_type": row.get("transaction_type"),
                "cl_barcode_status": row.get("barcode_status"),
                "cl_transaction_status": row.get("transaction_status"),
                "cl_updated_at": row.get("updated_at"),
            }
    except Exception:
        return out
    return out


def _detect_location_mismatch(
    cl_loc: Any,
    paas_loc: Any,
    saas_loc: Any,
) -> bool:
    cl = _norm_token(cl_loc)
    paas = _norm_token(paas_loc)
    saas = _norm_token(saas_loc)
    refs = [v for v in (cl, paas, saas) if v]
    if len(refs) < 2:
        return False
    first = refs[0]
    return any(not _values_match(first, other) for other in refs[1:])


def _detect_transaction_mismatch(
    cl_type: Any,
    cl_bstatus: Any,
    cl_tstatus: Any,
    paas_type: Any,
    paas_bstatus: Any,
    paas_tstatus: Any,
    saas_type: Any,
    saas_bstatus: Any,
    saas_tstatus: Any,
) -> bool:
    status_pairs = [
        (cl_bstatus, paas_bstatus),
        (cl_bstatus, saas_bstatus),
        (cl_tstatus, paas_tstatus),
        (cl_tstatus, saas_tstatus),
    ]
    for left, right in status_pairs:
        if _norm_token(left) and _norm_token(right) and not _values_match(left, right):
            return True

    type_pairs = [
        (cl_type, paas_type),
        (cl_type, saas_type),
    ]
    for left, right in type_pairs:
        if _norm_token(left) and _norm_token(right) and not _txn_types_align(left, right):
            return True
    return False


def _enrich_lines(header: dict, lines: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    """Merge invoice, manufacturing, barcode sync, and work-order data onto barcode rows."""
    from .fusion_db import (
        fetch_duplicate_onhand_by_barcodes,
        fetch_inhouse_bag_by_order,
        fetch_jw_grn_by_order,
        fetch_manufacturing_by_skus,
        fetch_paas_barcode_trx_by_barcodes,
        fetch_sold_onhand_by_barcodes,
        fetch_work_orders_by_sales_order,
        fusion_db_configured,
    )
    from .fusion_report import (
        lookup_barcode_saas_location,
        lookup_barcode_saas_transaction,
        lookup_item_atp_wd_uom,
        soap_configured as fusion_soap_configured,
    )

    entity_id = header.get("entity_id")
    hash_id = _str_val(header.get("hash_id"))
    invoice_map = _fetch_invoice_map(int(entity_id)) if entity_id else {}

    barcodes = list({_str_val(ln.get("barcode")) for ln in lines if _str_val(ln.get("barcode"))})
    skus = list({_str_val(ln.get("item_number")) for ln in lines if _str_val(ln.get("item_number"))})
    mfg_map = fetch_manufacturing_by_skus(skus) if skus else {}

    cl_barcode_map = _fetch_cl_barcode_attrs(barcodes)
    paas_map = fetch_paas_barcode_trx_by_barcodes(barcodes) if barcodes else {}
    dup_map = fetch_duplicate_onhand_by_barcodes(barcodes) if barcodes else {}
    sold_map = fetch_sold_onhand_by_barcodes(barcodes) if barcodes else {}

    saas_loc_map, saas_loc_state = lookup_barcode_saas_location(barcodes)
    saas_txn_map, saas_txn_state = lookup_barcode_saas_transaction(barcodes)

    inhouse_bags: dict[str, dict] = {}
    jw_grn_map: dict[str, dict] = {}
    work_orders: list[dict] = []
    if hash_id and fusion_db_configured():
        inhouse_bags = fetch_inhouse_bag_by_order(hash_id)
        jw_grn_map = fetch_jw_grn_by_order(hash_id)
        work_orders = fetch_work_orders_by_sales_order(hash_id)

    po_numbers = [
        _str_val(g.get("transaction_number"))
        for g in jw_grn_map.values()
        if _str_val(g.get("transaction_number"))
    ]
    vendor_qc_map = _fetch_vendor_qc_by_pos(po_numbers) if po_numbers else {}

    atp_map, atp_cache_state = lookup_item_atp_wd_uom(skus)

    enriched: list[dict] = []
    for ln in lines:
        row = dict(ln)
        bc = _str_val(row.get("barcode"))
        erp = _str_val(row.get("erp_status"))
        sku = _str_val(row.get("item_number"))

        # Invoice (primarily for Dispatched)
        inv = invoice_map.get(bc, {})
        if erp == "Dispatched":
            row["invoice_number"] = inv.get("invoice_number")
            row["invoice_barcode"] = inv.get("invoice_barcode")
            row["invoice_flag"] = inv.get("invoice_flag") or (
                "PENDING_INVOICE" if bc else None
            )
        else:
            row["invoice_number"] = inv.get("invoice_number")
            row["invoice_barcode"] = inv.get("invoice_barcode")
            row["invoice_flag"] = inv.get("invoice_flag")

        # Manufacturing type from Fusion (by SKU / item_number)
        mfg = mfg_map.get(sku, {})
        row["make_buy"] = mfg.get("make_buy")
        row["vendor_name"] = mfg.get("vendor_name")
        row["manufacturing_type"] = mfg.get("manufacturing_type")

        mtype = _str_val(row.get("manufacturing_type"))

        # Inhouse bag + metal loss (Fusion)
        if mtype == "Inhouse":
            bag = inhouse_bags.get(bc, {})
            row["parent_bag_no"] = bag.get("parent_bag_no")
            row["bag_status"] = bag.get("bag_status")
            row["factory"] = bag.get("factory")
            row["bag_weight"] = bag.get("bag_weight")
            row["loss_weight"] = bag.get("loss_weight")
            row["loss_weight_pg"] = bag.get("loss_weight_pg")
            row["loss_weight_pg_999"] = bag.get("loss_weight_pg_999")
            row["loss_stock_value"] = bag.get("loss_stock_value")
            row["bag_last_update"] = bag.get("bag_last_update")

        # JW vendor: GRN (Fusion) + Vendor QC (Magento, PO = transaction_number)
        if mtype == "JW":
            grn = jw_grn_map.get(bc, {})
            po = _str_val(grn.get("transaction_number"))
            row["grn_transaction_number"] = po or None
            row["grn_status"] = grn.get("grn_status")
            row["grn_gross_weight"] = grn.get("gross_weight")
            if po:
                qc = vendor_qc_map.get((bc, po), {})
                row["vendor_qc_status"] = qc.get("vendor_qc_status")
                row["vendor_qc_status_time"] = qc.get("vendor_qc_status_time")

        # Item ATP / Work Definition / UOM (Fusion BI report)
        item_status = atp_map.get(sku, {})
        row["atp_status"] = item_status.get("atp_status")
        row["wd_status"] = item_status.get("wd_status")
        row["uom_status"] = item_status.get("uom_status")

        # CL barcode attributes
        cl = cl_barcode_map.get(bc, {})
        row["cl_location_name"] = cl.get("cl_location_name")
        row["cl_transaction_type"] = cl.get("cl_transaction_type")
        row["cl_barcode_status"] = cl.get("cl_barcode_status")
        row["cl_transaction_status"] = cl.get("cl_transaction_status")
        row["cl_barcode_updated_at"] = cl.get("cl_updated_at")

        # PaaS barcode trx/loc
        paas = paas_map.get(bc, {})
        row["paas_organization_name"] = paas.get("organization_name")
        row["paas_transaction_type"] = paas.get("transaction_type_name")
        row["paas_barcode_status"] = paas.get("barcode_status")
        row["paas_transaction_status"] = paas.get("transaction_status")
        row["paas_last_update_date"] = paas.get("last_update_date")

        # SaaS reports
        saas_loc = saas_loc_map.get(bc, {})
        saas_txn = saas_txn_map.get(bc, {})
        row["saas_location_name"] = saas_loc.get("location_name")
        row["saas_transaction_type"] = saas_txn.get("transaction_type")
        row["saas_barcode_status"] = saas_txn.get("barcode_status")
        row["saas_transaction_status"] = saas_txn.get("transaction_status")

        row["location_mismatch"] = _detect_location_mismatch(
            row.get("cl_location_name") or row.get("location_name"),
            row.get("paas_organization_name"),
            row.get("saas_location_name"),
        )
        row["transaction_mismatch"] = _detect_transaction_mismatch(
            row.get("cl_transaction_type"),
            row.get("cl_barcode_status"),
            row.get("cl_transaction_status"),
            row.get("paas_transaction_type"),
            row.get("paas_barcode_status"),
            row.get("paas_transaction_status"),
            row.get("saas_transaction_type"),
            row.get("saas_barcode_status"),
            row.get("saas_transaction_status"),
        )

        dup_count = dup_map.get(bc)
        row["duplicate_onhand_count"] = dup_count
        row["sold_onhand_present"] = bc in sold_map

        enriched.append(_json_safe(row))

    integrations = {
        "invoice": True,
        "manufacturing": bool(mfg_map),
        "inhouse_bag": bool(inhouse_bags),
        "jw_grn": bool(jw_grn_map),
        "vendor_qc": bool(vendor_qc_map),
        "item_atp_wd_uom": atp_cache_state,
        "cl_barcode_attrs": bool(cl_barcode_map),
        "paas_barcode_trx": bool(paas_map),
        "saas_barcode_location": saas_loc_state,
        "saas_barcode_transaction": saas_txn_state,
        "duplicate_onhand": bool(dup_map),
        "sold_onhand": bool(sold_map),
        "work_orders": bool(work_orders),
        "fusion_db": fusion_db_configured(),
        "fusion_soap": fusion_soap_configured(),
    }
    return enriched, integrations, [_json_safe(wo) for wo in work_orders]


def _lookup_header(order_number: str) -> Optional[dict]:
    """Resolve order by increment_id, entity_id, hash_id, or ERP order id (indexed lookups)."""
    row = fetch_one(ORDER_LOOKUP_BY_INCREMENT, {"order_number": order_number})
    if row:
        return row
    if order_number.isdigit():
        eid = int(order_number)
        row = fetch_one(ORDER_LOOKUP_BY_ENTITY, {"entity_id": eid})
        if row:
            return row
        row = fetch_one(ORDER_LOOKUP_BY_ERP, {"entity_id": eid})
        if row:
            return row
    return fetch_one(ORDER_LOOKUP_BY_HASH, {"order_number": order_number})


def validate_order(order_number: str) -> ValidationResult:
    """First-check: locate order in Magento and evaluate checklist criteria."""
    order_number = order_number.strip()
    if not order_number:
        return ValidationResult(
            order_number=order_number,
            found=False,
            valid=False,
            message="Order number is required.",
        )

    if not db_configured():
        raise DatabaseConfigError(
            "Magento DB password not set. Add MAGENTO_DB_PASSWORD to .env"
        )

    header = _lookup_header(order_number)
    if not header:
        return ValidationResult(
            order_number=order_number,
            found=False,
            valid=False,
            message=f"Order '{order_number}' was not found in Magento.",
        )

    entity_id = header["entity_id"]
    raw_lines = fetch_all(ORDER_LINES_SQL, {"entity_id": entity_id})
    lines, integrations, work_orders = _enrich_lines(header, raw_lines)
    customer = _extract_customer(header, lines, order_number)
    checks = _run_pre_audit_checks(header, lines)
    all_passed = all(c.passed for c in checks)

    hash_id = _str_val(header.get("hash_id")) or None
    meta = _order_meta_line(header)

    if all_passed:
        message = (
            f"Order {order_number} passed all first-check criteria "
            f"({len(lines)} barcode line(s))."
        )
    else:
        failed = [c.label for c in checks if not c.passed]
        message = f"Order {order_number} found but failed: {', '.join(failed)}."

    return ValidationResult(
        order_number=order_number,
        found=True,
        valid=all_passed,
        order_id=order_number,
        hash_id=hash_id,
        entity_id=int(entity_id),
        checks=checks,
        customer=customer,
        header=_json_safe(header),
        lines=lines,
        line_count=len(lines),
        message=message,
        meta=meta,
        integrations=integrations,
        work_orders=work_orders,
    )


def rows_to_raw_order(header: dict, lines: list[dict]) -> dict:
    """Map Magento rows -> fixture-shaped dict for the audit engine."""
    if not lines:
        lines = [{}]

    first = lines[0]
    order_no = (
        _str_val(header.get("hash_id"))
        or str(header.get("entity_id", ""))
    )

    order_header = {
        "order_id": header.get("entity_id"),
        "order_no": order_no,
        "order_type": _str_val(first.get("item_order_type") or header.get("source")),
        "order_date": _str_val(header.get("created_at")),
        "customer_id": _str_val(header.get("fusion_party_number")),
        "customer_name": _customer_name(header, lines),
        "email": _customer_email(header, lines),
        "financial_approval": _str_val(first.get("financial_approval")),
        "payment_source": _str_val(first.get("source") or header.get("source")),
        "sub_total": _num(first.get("price_before_tax")),
        "tax": _num(first.get("tax")),
        "grand_total": _num(first.get("final_price")),
        "order_amount": _num(header.get("net_payable")),
        "total_qty_ordered": len(lines),
        "coupon_code": "",
        "discount_amount": 0.0,
        "ship_pin_code": "",
        "ship_state": _str_val(first.get("location_name")),
    }

    order_items = []
    for ln in lines:
        barcode = _str_val(ln.get("barcode"))
        pbt = _num(ln.get("price_before_tax"))
        tax = _num(ln.get("tax"))
        amount = _num(ln.get("final_price"))
        detail = {
            "sku": _str_val(ln.get("item_number") or ln.get("sku")),
            "barcode": barcode,
            "product_name": _str_val(ln.get("name")),
            "price": str(pbt) if pbt is not None else "",
            "price_before_tax": str(pbt) if pbt is not None else "",
            "tax": str(tax) if tax is not None else "",
            "amount": str(amount) if amount is not None else "",
            "status": _str_val(ln.get("erp_status")),
            "edd_date": _str_val(ln.get("expected_delivery_date_max")
                                  or ln.get("expected_delivery_date_min")),
            "discount_percent": "0.0",
            "flat_discount": "0.0",
            "is_diamond": False,
        }
        fulfillment = {
            "sku": _str_val(ln.get("item_number") or ln.get("sku")),
            "barcode": barcode,
            "product_name": _str_val(ln.get("name")),
            "price": str(pbt) if pbt is not None else "",
            "price_before_tax": str(pbt) if pbt is not None else "",
            "tax": str(tax) if tax is not None else "",
            "amount": str(amount) if amount is not None else "",
            "status": _str_val(ln.get("pick_status") or ln.get("erp_status")),
            "invoice_no": _str_val(ln.get("invoice_number") or ln.get("erp_doc_no")),
            "certificate_no": "",
            "is_diamond": False,
        }
        order_items.append({
            "pricing_reference": {
                "barcode": barcode,
                "computed_price": pbt,
            },
            "ORDER DETAIL": detail,
            "FULFILLMENT DETAIL": fulfillment,
        })

    shipping = []
    if _customer_email(header, lines) or _customer_name(header, lines):
        shipping.append({
            "firstname": _customer_name(header, lines),
            "email": _customer_email(header, lines),
            "region": _str_val(first.get("location_name")),
            "postcode": "",
            "country": "India",
        })

    return {
        "order_header": order_header,
        "order_items": order_items,
        "order_billing_address": shipping,
        "order_shipping_address": shipping,
        "_magento_meta": {
            "entity_id": header.get("entity_id"),
            "hash_id": header.get("hash_id"),
            "line_count": len(lines),
        },
    }


def fetch_order_raw(order_number: str) -> dict:
    """Fetch and map a Magento order for the audit loader."""
    result = validate_order(order_number)
    if not result.found:
        raise KeyError(result.message)
    return rows_to_raw_order(result.header, result.lines)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
