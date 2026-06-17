"""SOP escalation checks — CaratLane first-check playbook with team routing."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .magento import CheckResult

_STALE_DAYS = 90
_HIGH_VALUE_THRESHOLD = 500_000.0


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _truthy(value: Any) -> bool:
    return _str(value).lower() in {"1", "true", "yes", "y"}


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _str(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _atp_present(ln: dict) -> bool:
    return all(_str(ln.get(k)).upper() == "PRESENT" for k in ("atp_status", "wd_status", "uom_status"))


def run_sop_checks(
    header: dict,
    lines: list[dict],
    work_orders: list[dict],
    finance: dict,
) -> list["CheckResult"]:
    """Return SOP checks with escalation actions for failed cases."""
    from .magento import CheckResult

    ar_map: dict = finance.get("ar_by_invoice") or {}
    ap_map: dict = finance.get("ap_by_invoice") or {}
    rm_map: dict = finance.get("rm_by_wo") or {}

    checks: list[CheckResult] = []

    # 1. PAN empty → Service team
    pan = _str((lines[0].get("pan_no") if lines else "") or "")
    if not pan and lines:
        pan_vals = {_str(ln.get("pan_no")) for ln in lines if _str(ln.get("pan_no"))}
        pan = next(iter(pan_vals), "")
    checks.append(CheckResult(
        name="sop_pan_present",
        label="PAN on order",
        module="SOP",
        passed=bool(pan),
        expected="PAN present on customer/location",
        actual=pan or "empty",
        detail="Customer PAN required for invoicing and compliance.",
        action="" if pan else "Reach Service team",
    ))

    # 2. Order processing > 3 months
    created = _parse_date(header.get("created_at"))
    processing = [ln for ln in lines if _str(ln.get("erp_status")) == "Processing"]
    stale = False
    age_days = 0
    if created and processing:
        age_days = (date.today() - created).days
        stale = age_days > _STALE_DAYS
    checks.append(CheckResult(
        name="sop_order_not_stale",
        label="Order not processing > 3 months",
        module="SOP",
        passed=not stale,
        expected=f"created_at within {_STALE_DAYS} days if still Processing",
        actual=(
            f"Processing for {age_days} days (created {created})"
            if stale else (
                f"{age_days} days since {created}" if created else "not stale / not Processing"
            )
        ),
        detail="Compare sales_flat_order.created_at with today; alert if Processing > 90 days.",
        action="Order stuck in Processing > 3 months — escalate to Service/ERP" if stale else "",
    ))

    # 3. Manufacturing barcode not reserved
    unreserved = [
        _str(ln.get("barcode"))
        for ln in lines
        if _str(ln.get("manufacturing_type")) in {"Inhouse", "JW", "Make"}
        and not _truthy(ln.get("barcode_reserved_in_fusion"))
    ]
    unreserved = [b for b in unreserved if b]
    checks.append(CheckResult(
        name="sop_barcode_reserved",
        label="Manufacturing barcode reserved in Fusion",
        module="SOP",
        passed=len(unreserved) == 0,
        expected="barcode_reserved_in_fusion = 1 for mfg lines",
        actual=", ".join(unreserved) if unreserved else "all reserved",
        detail="Make/Inhouse/JW barcodes must be reserved before fulfillment.",
        action="Barcode manufacturing but not reserved — Service / Buy team" if unreserved else "",
    ))

    # 4. MTO without barcode
    mto_missing = [
        _str(ln.get("item_number"))
        for ln in lines
        if "mto" in _str(ln.get("order_processing_type")).lower()
        and not _str(ln.get("barcode"))
    ]
    mto_missing = [x for x in mto_missing if x]
    checks.append(CheckResult(
        name="sop_mto_has_barcode",
        label="MTO order has barcode",
        module="SOP",
        passed=len(mto_missing) == 0,
        expected="barcode generated for MTO (Make-to-Order) lines",
        actual=", ".join(mto_missing) if mto_missing else "all MTO lines have barcode",
        detail="Sales order is MTO but barcode not yet generated.",
        action="MTO without barcode — ERP / Service team" if mto_missing else "",
    ))

    # 5. PO not generated (JW / JOB WORK)
    missing_po = [
        _str(wo.get("work_order_number"))
        for wo in work_orders
        if _str(wo.get("manufacturing_type")).upper() in {"JOB WORK", "JW"}
        and not _str(wo.get("po_number"))
    ]
    checks.append(CheckResult(
        name="sop_po_generated",
        label="PO generated (JW / Buy)",
        module="SOP",
        passed=len(missing_po) == 0,
        expected="PO_NUMBER on JOB WORK work orders",
        actual=", ".join(missing_po) if missing_po else "PO present or N/A (inhouse)",
        detail="Job-work manufacturing requires a purchase order in Fusion.",
        action="PO not generated — Buy team" if missing_po else "",
    ))

    # 6. ATP / WD / UOM missing
    atp_missing = [
        f"{_str(ln.get('barcode'))} ({_str(ln.get('item_number'))})"
        for ln in lines
        if not _atp_present(ln)
    ]
    checks.append(CheckResult(
        name="sop_atp_wd_uom",
        label="ATP / WD / UOM present",
        module="SOP",
        passed=len(atp_missing) == 0,
        expected="ATP, WD, UOM = PRESENT for each SKU",
        actual=", ".join(atp_missing[:5]) + (f" (+{len(atp_missing)-5})" if len(atp_missing) > 5 else "")
        if atp_missing else "all PRESENT",
        detail="Item master ATP/WD/UOM status from Fusion BI report.",
        action="ATP/WD/UOM missing — ERP team" if atp_missing else "",
    ))

    # 7. Metal / loss weight / loss stock (Inhouse) — disabled until required
    # loss_missing = [
    #     _str(ln.get("barcode"))
    #     for ln in lines
    #     if _str(ln.get("manufacturing_type")) == "Inhouse"
    #     and (
    #         _num(ln.get("loss_weight")) is None
    #         or _num(ln.get("loss_stock_value")) is None
    #     )
    # ]
    # checks.append(CheckResult(
    #     name="sop_metal_loss_values",
    #     label="Inhouse metal loss values",
    #     ...

    # 8. QC reject / GRN status
    qc_issues: list[str] = []
    for ln in lines:
        bc = _str(ln.get("barcode"))
        qc = _str(ln.get("vendor_qc_status")).lower()
        grn = _str(ln.get("grn_status")).upper()
        if qc and "reject" in qc:
            qc_issues.append(f"{bc}: QC {ln.get('vendor_qc_status')}")
        elif grn and grn not in {"", "DONE", "COMPLETE", "COMPLETED", "RECEIVED", "CLOSED"}:
            if _str(ln.get("manufacturing_type")) == "JW":
                qc_issues.append(f"{bc}: GRN {ln.get('grn_status')}")
    checks.append(CheckResult(
        name="sop_qc_grn",
        label="QC / GRN status",
        module="SOP",
        passed=len(qc_issues) == 0,
        expected="No QC reject; GRN done for JW lines",
        actual="; ".join(qc_issues) if qc_issues else "QC pass / GRN done",
        detail="Vendor QC reject or GRN not completed.",
        action="QC reject or GRN pending — Service / Buy team" if qc_issues else "",
    ))

    # 9. RM consumption per work order (SaaS)
    rm_issues: list[str] = []
    if not work_orders:
        rm_actual = "no work orders on order"
        rm_passed = True
    elif not finance.get("enabled"):
        rm_actual = "SaaS finance reports disabled"
        rm_passed = True
    else:
        for wo in work_orders:
            won = _str(wo.get("work_order_number"))
            if not won:
                continue
            rm = rm_map.get(won, {})
            consumed = rm.get("consumed")
            if consumed is False:
                rm_issues.append(won)
            elif consumed is None and rm.get("error"):
                rm_issues.append(f"{won} (SaaS error)")
        rm_actual = ", ".join(rm_issues) if rm_issues else "consumption recorded"
        rm_passed = len(rm_issues) == 0
    checks.append(CheckResult(
        name="sop_rm_consumption",
        label="Work order RM consumption",
        module="SOP",
        passed=rm_passed,
        expected="RM consumption recorded in Fusion (SaaS report returns rows)",
        actual=rm_actual,
        detail="SaaS Work_order_rm_consumption_RPT — empty = not consumed.",
        action="RM consumption not happened — Manufacturing / ERP team" if rm_issues else "",
    ))

    # 10. Work order completion (BUY / JOB WORK) — disabled until actual WO status is used
    # wo_open: list[str] = []
    # for wo in work_orders:
    #     ...
    # checks.append(CheckResult(
    #     name="sop_wo_completion_buy",
    #     label="Work order completion (Buy)",
    #     ...

    # 11–12. AP invoice unpaid / ledger id empty — disabled until required
    # ap_unpaid: list[str] = []
    # ledger_empty: list[str] = []
    # for inv, rec in ap_map.items():
    #     status = _str(rec.get("payment_status")).lower()
    #     if status in {"unpaid", "partially paid"} or _str(rec.get("payment_status_flag")).upper() == "N":
    #         ap_unpaid.append(f"{inv} ({rec.get('payment_status') or 'Unpaid'})")
    #     if not _str(rec.get("ledger_id")):
    #         ledger_empty.append(inv)
    # checks.append(CheckResult(
    #     name="sop_ap_invoice_paid",
    #     label="AP invoice payment status",
    #     module="SOP",
    #     passed=len(ap_unpaid) == 0,
    #     expected="AP invoices Paid (SaaS AP_Inovice_status_RPT)",
    #     actual=", ".join(ap_unpaid) if ap_unpaid else "paid or none",
    #     detail="Unpaid AP invoices block vendor settlement.",
    #     action="AP invoice unpaid — Finance / Buy team" if ap_unpaid else "",
    # ))
    # checks.append(CheckResult(
    #     name="sop_ledger_id",
    #     label="AP ledger ID present",
    #     module="SOP",
    #     passed=len(ledger_empty) == 0,
    #     expected="LEDGER_ID populated on AP invoices",
    #     actual=", ".join(ledger_empty) if ledger_empty else "ledger linked",
    #     detail="Ledger entry required through AP invoice posting.",
    #     action="Ledger ID empty — Finance / ERP team" if ledger_empty else "",
    # ))

    # 13. AR invoice open
    ar_open: list[str] = []
    for inv, rec in ar_map.items():
        status = _str(rec.get("invoice_status") or rec.get("status")).upper()
        remaining = _num(rec.get("amount_due_remaining")) or 0
        if status not in {"CLOSED", "CL"} and remaining > 0:
            ar_open.append(f"{inv} ({status}, due ₹{remaining:,.0f})")
        elif status not in {"CLOSED", "CL"} and status:
            ar_open.append(f"{inv} ({status})")
    checks.append(CheckResult(
        name="sop_ar_invoice_open",
        label="AR invoice status",
        module="SOP",
        passed=len(ar_open) == 0,
        expected="AR invoices CLOSED (SaaS AR_invoices_RPT)",
        actual=", ".join(ar_open) if ar_open else "closed or none",
        detail="Open AR invoice indicates billing/receivables gap.",
        action="AR invoice open — Finance / ERP team" if ar_open else "",
    ))

    # 14. Profile balance & partial ILO — not implemented
    checks.append(CheckResult(
        name="sop_profile_balance_ilo",
        label="Profile balance & Transfer (partial ILO)",
        module="SOP",
        passed=True,
        expected="ILO profile balance transfer reconciliation",
        actual="Not implemented — pending ILO integration",
        detail="Placeholder SOP check; integration not yet available.",
        action="Not implemented",
    ))

    # 15. High-value order (> ₹500,000)
    net = _num(header.get("net_payable"))
    high_value = net is not None and net > _HIGH_VALUE_THRESHOLD
    checks.append(CheckResult(
        name="sop_high_value_order",
        label="High-value order flag",
        module="SOP",
        passed=True,
        expected=f"net_payable ≤ ₹{_HIGH_VALUE_THRESHOLD:,.0f} or flagged for review",
        actual=f"HIGH VALUE ₹{net:,.0f}" if high_value else f"₹{net:,.0f}" if net else "—",
        detail="Orders above ₹5L require additional financial approval scrutiny.",
        action="High-value order (>₹5L) — Finance review required" if high_value else "",
    ))

    return checks
