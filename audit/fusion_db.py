"""Oracle Fusion (wksp_xxcl) read-only connection for manufacturing & inhouse bag data."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

ORACLE_IN_LIST_CHUNK = 900


class FusionDatabaseConfigError(RuntimeError):
    """Raised when Fusion/Oracle DB settings are missing."""


def fusion_db_configured() -> bool:
    return bool(_user() and _password() and _dsn())


def _user() -> str:
    return os.getenv("ORACLE_USER", "").strip()


def _password() -> str:
    return os.getenv("ORACLE_PASSWORD", "").strip()


def _dsn() -> str:
    return os.getenv("ORACLE_DSN", "").strip()


def _resolve_wallet() -> Optional[Path]:
    explicit = os.getenv("ORACLE_WALLET_DIR", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    tns = os.getenv("TNS_ADMIN", "").strip()
    if tns:
        candidates.append(Path(tns))
    candidates.extend([
        Path("/Users/manikandanms/Documents/Wallet_CARATLANEOICATPPROD/Wallet_CARATLANEOICATPPROD"),
        Path("/Users/manikandanms/Documents/Wallet_CARATLANEOICATPPROD"),
    ])
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_dir() and (resolved / "tnsnames.ora").is_file():
            return resolved
    return None


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    if not fusion_db_configured():
        raise FusionDatabaseConfigError(
            "Fusion DB not configured. Set ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN in .env"
        )
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError(
            "oracledb is required for manufacturing lookups. pip install oracledb"
        ) from exc

    wallet = _resolve_wallet()
    if wallet is not None:
        os.environ["TNS_ADMIN"] = str(wallet)

    conn = oracledb.connect(user=_user(), password=_password(), dsn=_dsn())
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(sql: str, params: Optional[dict] = None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_manufacturing_by_skus(skus: list[str]) -> dict[str, dict]:
    """Return item_number -> {make_buy, vendor_name, manufacturing_type}."""
    cleaned = sorted({s.strip() for s in skus if s and str(s).strip()})
    if not cleaned or not fusion_db_configured():
        return {}

    out: dict[str, dict] = {}
    try:
        import oracledb
    except ImportError:
        return {}

    sql_base = """
        SELECT DISTINCT
            im.ITEM_NUMBER,
            im.MAKE_BUY,
            xpsv.VENDOR_NAME,
            CASE
                WHEN im.MAKE_BUY = 'Buy' THEN 'Outright'
                WHEN xpsv.VENDOR_NAME LIKE 'CL-FA%%' THEN 'Inhouse'
                ELSE 'JW'
            END AS MANUFACTURING_TYPE
        FROM wksp_xxcl.XXCL_ITEM_STRUCTURE_DTL_V xisdv
        LEFT JOIN wksp_xxcl.xxcl_item_master im
            ON xisdv.ASSEMBLY_NUM = im.ITEM_NUMBER
        LEFT JOIN wksp_xxcl.XXCL_EGO_ITEM_EFF_B xeie
            ON im.INVENTORY_ITEM_ID = xeie.INVENTORY_ITEM_ID
        LEFT JOIN wksp_xxcl.XXCL_POZ_SUPPLIER_SITES_V xpss
            ON xeie.ATTRIBUTE_NUMBER1 = xpss.VENDOR_SITE_ID
        LEFT JOIN wksp_xxcl.XXCL_POZ_SUPPLIERS_V xpsv
            ON xpss.VENDOR_ID = xpsv.VENDOR_ID
        WHERE im.ITEM_CLASS_NAME LIKE 'FG%%'
        AND xpsv.VENDOR_NAME IS NOT NULL
        AND im.ITEM_NUMBER IN ({placeholders})
        ORDER BY im.ITEM_NUMBER
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for offset in range(0, len(cleaned), ORACLE_IN_LIST_CHUNK):
                    chunk = cleaned[offset: offset + ORACLE_IN_LIST_CHUNK]
                    binds = {f"s{i}": v for i, v in enumerate(chunk)}
                    placeholders = ", ".join(f":s{i}" for i in range(len(chunk)))
                    cur.execute(sql_base.format(placeholders=placeholders), binds)
                    cols = [d[0] for d in cur.description]
                    for row in cur.fetchall():
                        rec = dict(zip(cols, row))
                        key = str(rec.get("ITEM_NUMBER", "")).strip()
                        if key:
                            out[key] = {
                                "item_number": key,
                                "make_buy": rec.get("MAKE_BUY"),
                                "vendor_name": rec.get("VENDOR_NAME"),
                                "manufacturing_type": rec.get("MANUFACTURING_TYPE"),
                            }
    except Exception:
        return out
    return out


def fetch_inhouse_bag_by_order(order_number: str) -> dict[str, dict]:
    """Return barcode -> bag/metal-loss details for inhouse manufacturing."""
    order_number = (order_number or "").strip()
    if not order_number or not fusion_db_configured():
        return {}

    sql = """
        SELECT
            x.BARCODE_NUMBER,
            x.PARENT_BAG_NO,
            x.BAG_STATUS,
            x.FACTORY,
            x.BAG_WEIGHT,
            x.LAST_UPDATE_DATE,
            SUM(xmlr.LOSS_WEIGHT)        AS LOSS_WEIGHT,
            SUM(xmlr.LOSS_WEIGHT_PG)     AS LOSS_WEIGHT_PG,
            SUM(xmlr.LOSS_WEIGHT_PG_999) AS LOSS_WEIGHT_PG_999,
            SUM(xmlr.LOSS_STOCK_VALUE)   AS LOSS_STOCK_VALUE
        FROM (
            SELECT *
            FROM (
                SELECT
                    xmbh.HEADER_ID,
                    xmbh.PARENT_BAG_NO,
                    xmbh.ORDER_NUMBER,
                    xmbh.BARCODE_NUMBER,
                    xmbh.BAG_STATUS,
                    xmbh.FACTORY,
                    xmbh.BAG_WEIGHT,
                    xmbh.LAST_UPDATE_DATE,
                    ROW_NUMBER() OVER (
                        PARTITION BY xmbh.BARCODE_NUMBER
                        ORDER BY xmbh.HEADER_ID DESC
                    ) rn
                FROM wksp_xxcl.XXCL_MFG_BAG_HEADER xmbh
                WHERE xmbh.ORDER_NUMBER = :order_number
            )
            WHERE rn = 1
        ) x
        LEFT JOIN wksp_xxcl.XXCL_MFG_METAL_LOSS_RPT xmlr
            ON x.PARENT_BAG_NO = xmlr.BAG_NUMBER
        GROUP BY
            x.BARCODE_NUMBER,
            x.PARENT_BAG_NO,
            x.BAG_STATUS,
            x.FACTORY,
            x.BAG_WEIGHT,
            x.LAST_UPDATE_DATE
    """

    out: dict[str, dict] = {}
    try:
        rows = fetch_all(sql, {"order_number": order_number})
        for row in rows:
            bc = str(row.get("BARCODE_NUMBER") or "").strip()
            if bc:
                out[bc] = {
                    "parent_bag_no": row.get("PARENT_BAG_NO"),
                    "bag_status": row.get("BAG_STATUS"),
                    "factory": row.get("FACTORY"),
                    "bag_weight": row.get("BAG_WEIGHT"),
                    "loss_weight": row.get("LOSS_WEIGHT"),
                    "loss_weight_pg": row.get("LOSS_WEIGHT_PG"),
                    "loss_weight_pg_999": row.get("LOSS_WEIGHT_PG_999"),
                    "loss_stock_value": row.get("LOSS_STOCK_VALUE"),
                    "bag_last_update": row.get("LAST_UPDATE_DATE"),
                }
    except Exception:
        return out
    return out


def fetch_jw_grn_by_order(sales_order_no: str) -> dict[str, dict]:
    """Return barcode -> GRN details for JW vendor lines (Fusion)."""
    sales_order_no = (sales_order_no or "").strip()
    if not sales_order_no or not fusion_db_configured():
        return {}

    sql = """
        SELECT
            TRANSACTION_NUMBER,
            BARCODE,
            GROSS_WEIGHT,
            GRN_STATUS
        FROM wksp_xxcl.xxcl_vndr_add_grn
        WHERE SALES_ORDER_NO = :sales_order_no
        ORDER BY BARCODE
    """

    out: dict[str, dict] = {}
    try:
        rows = fetch_all(sql, {"sales_order_no": sales_order_no})
        for row in rows:
            bc = str(row.get("BARCODE") or "").strip()
            if not bc:
                continue
            txn = row.get("TRANSACTION_NUMBER")
            existing = out.get(bc)
            if not existing or (txn and not existing.get("transaction_number")):
                out[bc] = {
                    "transaction_number": txn,
                    "gross_weight": row.get("GROSS_WEIGHT"),
                    "grn_status": row.get("GRN_STATUS"),
                }
    except Exception:
        return out
    return out
