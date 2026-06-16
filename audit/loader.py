"""DATA COLLECTION: get an order snapshot and normalize it for the rules.

Three sources, one normalizer:
  1. load_fixture(order_id)  -> data/orders/*.json   (demo default, zero cost)
  2. fetch_from_oic(order_id)-> live OIC/Fusion REST  (stub; fill in endpoint+auth)
  3. (prod) the oneview_webhook 'order placed' event feeds the same raw dict here.

Swapping fixtures for live data is a one-function change because everything
downstream consumes the normalized `Order`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .schema import Item, Order, normalize_order_type

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "orders"


def _index_fixtures() -> dict[str, Path]:
    """Map order_id -> file path for every fixture on disk."""
    index: dict[str, Path] = {}
    if not DATA_DIR.exists():
        return index
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        oid = str(raw.get("order_header", {}).get("order_no") or
                  raw.get("order_header", {}).get("order_id") or path.stem)
        index[oid] = path
    return index


def list_order_ids() -> list[str]:
    return list(_index_fixtures().keys())


def load_fixture(order_id: str) -> dict:
    index = _index_fixtures()
    if order_id not in index:
        raise KeyError(
            f"Order '{order_id}' not found. Available: {', '.join(index) or '(none)'}"
        )
    return json.loads(index[order_id].read_text())


def fetch_from_oic(order_id: str) -> dict:
    """Live fetch stub. Wire the real Fusion/OIC REST call here when available.

    Expected to return the same raw shape as the fixtures (order_header/order_items/...).
    """
    base = os.getenv("OIC_BASE_URL")
    if not base:
        raise RuntimeError("OIC_BASE_URL not set; use load_fixture for the demo.")
    # import requests; r = requests.get(f"{base}/orders/{order_id}",
    #     headers={"Authorization": os.getenv("OIC_AUTH_HEADER", "")}); r.raise_for_status()
    # return r.json()
    raise NotImplementedError("Plug the OIC endpoint here.")


def _normalize_items(raw_items: list[dict]) -> list[Item]:
    items: list[Item] = []
    for entry in raw_items or []:
        items.append(Item(
            detail=entry.get("ORDER DETAIL", {}) or {},
            fulfillment=entry.get("FULFILLMENT DETAIL", {}) or {},
            pricing_reference=entry.get("pricing_reference", {}) or {},
        ))
    return items


def normalize(raw: dict) -> Order:
    """Raw CaratLane order dict -> tidy Order the rules consume."""
    header = raw.get("order_header", {}) or {}
    order_id = str(header.get("order_no") or header.get("order_id") or "UNKNOWN")
    order_type = normalize_order_type(header.get("order_type"), header.get("payment_source"))
    return Order(
        order_id=order_id,
        order_type=order_type,
        header=header,
        items=_normalize_items(raw.get("order_items", [])),
        billing=raw.get("order_billing_address", []) or [],
        shipping=raw.get("order_shipping_address", []) or [],
        raw=raw,
    )


def collect(order_id: str, source: str = "fixture") -> Order:
    """Single entry point used by the engine/UI."""
    if source == "oic":
        raw = fetch_from_oic(order_id)
    else:
        raw = load_fixture(order_id)
    return normalize(raw)
