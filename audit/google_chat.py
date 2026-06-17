"""Google Chat webhook notifications for Eagle Eye validation results."""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 3800
_HIGH_VALUE_THRESHOLD = 500_000.0


def webhook_configured() -> bool:
    return bool(os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "").strip())


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clip(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n… (truncated)"


def _order_value(result: Any) -> Optional[float]:
    header = getattr(result, "header", None) or {}
    if isinstance(header, dict):
        raw = header.get("net_payable")
    else:
        raw = None
    if raw is None:
        for ln in getattr(result, "lines", None) or []:
            if isinstance(ln, dict) and ln.get("net_payable") is not None:
                raw = ln.get("net_payable")
                break
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _format_inr(amount: float) -> str:
    return f"₹{amount:,.0f}"


def _check_status(check: Any) -> str:
    if not getattr(check, "passed", True):
        return "fail"
    action = _str(getattr(check, "action", ""))
    if action and action not in {"Not implemented", ""}:
        return "warn"
    return "pass"


def build_validation_message(result: Any) -> Optional[str]:
    """Build Google Chat summary for every validation search."""
    checks = list(getattr(result, "checks", None) or [])
    if not checks:
        return None

    order_no = _str(getattr(result, "order_number", ""))
    hash_id = _str(getattr(result, "hash_id", ""))
    entity_id = getattr(result, "entity_id", "")
    customer = getattr(result, "customer", {}) or {}
    valid = getattr(result, "valid", False)

    failed = [c for c in checks if _check_status(c) == "fail"]
    warnings = [c for c in checks if _check_status(c) == "warn"]
    passed_count = len(checks) - len(failed) - len(warnings)

    order_val = _order_value(result)
    high_value = order_val is not None and order_val > _HIGH_VALUE_THRESHOLD

    parts = [
        f"🦅 *Eagle Eye — {'PASS' if valid else 'FAIL'}*",
        f"Order: `{order_no}`",
    ]
    if hash_id:
        parts.append(f"Hash: `{hash_id}` · entity_id: `{entity_id}`")
    if customer.get("name") or customer.get("email"):
        parts.append(
            f"Customer: {_str(customer.get('name')) or '—'} · {_str(customer.get('email')) or '—'}"
        )
    if order_val is not None:
        val_line = f"Order value: *{_format_inr(order_val)}*"
        if high_value:
            val_line += " · ⚠️ *HIGH VALUE ORDER (> ₹5L)* — Finance review required"
        parts.append(val_line)
    parts.append(
        f"Checks: *{passed_count} passed* · *{len(failed)} failed* · *{len(warnings)} warning*"
    )
    parts.append("")

    if failed:
        parts.append(f"*Failed ({len(failed)}):*")
        for check in failed:
            parts.append(f"✗ *[{_str(check.module)}] {_str(check.label)}*")
            parts.append(f"  Actual: {_str(check.actual) or '—'}")
            action = _str(getattr(check, "action", ""))
            if action:
                parts.append(f"  ➜ *Action:* {action}")
            elif _str(check.detail):
                parts.append(f"  Detail: {_str(check.detail)}")
            parts.append("")

    if warnings:
        parts.append(f"*Warnings ({len(warnings)}):*")
        for check in warnings:
            parts.append(f"⚠ *[{_str(check.module)}] {_str(check.label)}*")
            parts.append(f"  {_str(check.actual) or '—'}")
            action = _str(getattr(check, "action", ""))
            if action:
                parts.append(f"  ➜ {action}")
            parts.append("")

    if not failed and not warnings:
        parts.append("✅ All checks passed.")

    return _clip("\n".join(parts))


def send_google_chat(text: str) -> None:
    url = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is required for Google Chat webhook") from exc
    resp = requests.post(url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def notify_validation_result(result: Any, *, async_send: bool = True) -> None:
    """Post validation status to Google Chat on every order search."""
    if not webhook_configured():
        return
    text = build_validation_message(result)
    if not text:
        return

    def _send() -> None:
        try:
            send_google_chat(text)
            logger.info("Google Chat status sent for order %s", getattr(result, "order_number", "?"))
        except Exception as exc:
            logger.warning("Google Chat webhook failed: %s", exc)

    if async_send:
        threading.Thread(target=_send, daemon=True).start()
    else:
        _send()


build_validation_alert = build_validation_message
