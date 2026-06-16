"""Normalized order model + safe parsing helpers.

The real CaratLane order JSON (see erp_data_sync/validate_order_json2.yml) nests
each line item as two blocks:  "ORDER DETAIL" (what was ordered/priced) and
"FULFILLMENT DETAIL" (what was actually fulfilled/invoiced). The audit needs both
so it can detect sub-inventory-transfer / sync mismatches between them.

This module turns the raw dict into a tidy structure the rules can rely on, and
gives every rule a single safe place to parse money strings like '17028.2' or ''.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def to_float(value: Any) -> Optional[float]:
    """Parse the messy money/number fields. '17028.2' -> 17028.2, '' / None -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s == "" or s.lower() in {"na", "null", "none"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_blank(value: Any) -> bool:
    """True for None, '', whitespace, or common null sentinels."""
    if value is None:
        return True
    s = str(value).strip()
    return s == "" or s.lower() in {"na", "null", "none"}


@dataclass
class Item:
    """One line item, holding both detail and fulfillment views + pricing reference."""
    detail: dict = field(default_factory=dict)         # ORDER DETAIL
    fulfillment: dict = field(default_factory=dict)    # FULFILLMENT DETAIL
    pricing_reference: dict = field(default_factory=dict)  # barcode BOM source of truth

    # convenience accessors (read from ORDER DETAIL, the authoritative ordered view)
    @property
    def sku(self) -> Any: return self.detail.get("sku")
    @property
    def barcode(self) -> Any: return self.detail.get("barcode")
    @property
    def product_name(self) -> Any: return self.detail.get("product_name")
    @property
    def is_diamond(self) -> bool: return bool(self.detail.get("is_diamond"))

    def f(self, key: str) -> Optional[float]:
        """Float from ORDER DETAIL."""
        return to_float(self.detail.get(key))

    def ff(self, key: str) -> Optional[float]:
        """Float from FULFILLMENT DETAIL."""
        return to_float(self.fulfillment.get(key))


@dataclass
class Order:
    order_id: str
    order_type: str                 # normalized: EZ / JM / JR / ONLINE / OLDGOLD
    header: dict = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    billing: list[dict] = field(default_factory=list)
    shipping: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def h(self, key: str) -> Optional[float]:
        """Float from header."""
        return to_float(self.header.get(key))


# --- order-type normalization -------------------------------------------------
# Maps the many real-world order_type / channel strings to our 5 buckets.
def normalize_order_type(raw_type: Any, raw_source: Any = None) -> str:
    blob = f"{raw_type or ''} {raw_source or ''}".upper()
    if "OLD" in blob and "GOLD" in blob:
        return "OLDGOLD"
    if blob.startswith("EZ") or " EZ" in blob or "EXCHANGE" in blob:
        return "EZ"
    if "ONLINE" in blob or "WEB" in blob or "ECOM" in blob:
        return "ONLINE"
    if blob.startswith("JR") or "REPAIR" in blob:
        return "JR"
    if blob.startswith("JM") or "STORE" in blob or "SOR" in blob:
        return "JM"
    return "JM"  # safe default: store order
