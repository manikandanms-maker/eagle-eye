"""Magento order lookup + first-check validation.

Uses the CaratLane sales_flat_order join graph. The base query mirrors the
hackathon checklist; validate_order() applies each criterion explicitly so
the UI can show pass/fail per check even when the order exists but is not
audit-eligible yet.
"""
from __future__ import annotations

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

INVOICE_BY_ORDER_SQL = """
SELECT
  sfoiq.barcode,
  sfoiq.erp_status,
  ci.invoice_number,
  ci.invoice_barcode,
  CASE
    WHEN ci.invoice_number IS NOT NULL THEN 'INVOICED'
    ELSE 'PENDING_INVOICE'
  END AS invoice_flag
FROM sales_flat_order sfo
JOIN sales_flat_order_item sfoi ON sfo.entity_id = sfoi.order_id
JOIN sales_flat_order_item_qty sfoiq ON sfoiq.item_id = sfoi.item_id
LEFT JOIN invoice_job_queue ijq ON ijq.order_id = sfo.entity_id
LEFT JOIN (
  SELECT
    ci_inner.*,
    REPLACE(
      REPLACE(
        SUBSTRING_INDEX(
          SUBSTRING_INDEX(
            JSON_UNQUOTE(JSON_EXTRACT(ci_inner.meta, '$.barcodeInfo')),
            '=>',
            1
          ),
          '{',
          -1
        ),
        '"', ''
      ),
      '\\\\', ''
    ) AS invoice_barcode
  FROM caratlane_invoices ci_inner
) ci ON ci.ijq_id = ijq.id AND ci.invoice_barcode = sfoiq.barcode
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


def _check_fusion_party(header: dict, lines: list[dict]) -> CheckResult:
    party = _str_val(header.get("fusion_party_number")
                     or (lines[0].get("fusion_party_number") if lines else ""))
    return CheckResult(
        name="fusion_party_number", label="Fusion party number", module="CUSTOMER",
        passed=bool(party),
        expected="non-empty fusion_party_number", actual=party or "empty",
        detail="Customer must be linked to an ERP Fusion party for billing sync.",
    )


PRE_AUDIT_RULES: list[Callable[[dict, list[dict]], CheckResult]] = [
    _check_fusion_order,
    _check_is_pushed,
    _check_non_test_email,
    _check_barcode_present,
    _check_erp_status,
    _check_customer_name,
    _check_customer_email,
    _check_fusion_party,
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


def _enrich_lines(header: dict, lines: list[dict]) -> tuple[list[dict], dict]:
    """Merge invoice, manufacturing, inhouse bag, and JW vendor data onto barcode rows."""
    from .fusion_db import (
        fetch_inhouse_bag_by_order,
        fetch_jw_grn_by_order,
        fetch_manufacturing_by_skus,
        fusion_db_configured,
    )

    entity_id = header.get("entity_id")
    hash_id = _str_val(header.get("hash_id"))
    invoice_map = _fetch_invoice_map(int(entity_id)) if entity_id else {}

    skus = list({_str_val(ln.get("item_number")) for ln in lines if _str_val(ln.get("item_number"))})
    mfg_map = fetch_manufacturing_by_skus(skus) if skus else {}

    inhouse_bags: dict[str, dict] = {}
    jw_grn_map: dict[str, dict] = {}
    if hash_id and fusion_db_configured():
        inhouse_bags = fetch_inhouse_bag_by_order(hash_id)
        jw_grn_map = fetch_jw_grn_by_order(hash_id)

    po_numbers = [
        _str_val(g.get("transaction_number"))
        for g in jw_grn_map.values()
        if _str_val(g.get("transaction_number"))
    ]
    vendor_qc_map = _fetch_vendor_qc_by_pos(po_numbers) if po_numbers else {}

    from .fusion_report import lookup_item_atp_wd_uom, soap_configured as fusion_soap_configured
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

        # Item ATP / Work Definition / UOM (Fusion BI report cache)
        item_status = atp_map.get(sku, {})
        row["atp_status"] = item_status.get("atp_status")
        row["wd_status"] = item_status.get("wd_status")
        row["uom_status"] = item_status.get("uom_status")

        enriched.append(_json_safe(row))

    integrations = {
        "invoice": True,
        "manufacturing": bool(mfg_map),
        "inhouse_bag": bool(inhouse_bags),
        "jw_grn": bool(jw_grn_map),
        "vendor_qc": bool(vendor_qc_map),
        "item_atp_wd_uom": atp_cache_state,
        "fusion_db": fusion_db_configured(),
        "fusion_soap": fusion_soap_configured(),
    }
    return enriched, integrations


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
    lines, integrations = _enrich_lines(header, raw_lines)
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
