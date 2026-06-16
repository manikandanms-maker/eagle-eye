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
    except Exception:
        return out
    return out


def _fetch_by_barcode_chunks(
    sql_template: str,
    barcodes: list[str],
    *,
    bind_prefix: str = "b",
) -> list[dict]:
    """Run an IN-list query in chunks; sql_template must contain {placeholders}."""
    cleaned = sorted({str(b).strip() for b in barcodes if str(b).strip()})
    if not cleaned or not fusion_db_configured():
        return []

    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for offset in range(0, len(cleaned), ORACLE_IN_LIST_CHUNK):
                    chunk = cleaned[offset: offset + ORACLE_IN_LIST_CHUNK]
                    binds = {f"{bind_prefix}{i}": v for i, v in enumerate(chunk)}
                    placeholders = ", ".join(f":{bind_prefix}{i}" for i in range(len(chunk)))
                    cur.execute(sql_template.format(placeholders=placeholders), binds)
                    cols = [d[0] for d in cur.description]
                    rows.extend(dict(zip(cols, row)) for row in cur.fetchall())
    except Exception:
        return rows
    return rows


def fetch_paas_barcode_trx_by_barcodes(barcodes: list[str]) -> dict[str, dict]:
    """Latest PaaS barcode trx/location row per barcode (XXCL_BARCODE_TRX_LOC_DETAILS)."""
    sql = """
        SELECT
            ranked.BARCODE_NUMBER,
            ranked.TRANSACTION_TYPE_NAME,
            ranked.ORGANIZATION_NAME,
            ranked.BARCODE_STATUS,
            ranked.TRANSACTION_STATUS,
            ranked.LAST_UPDATE_DATE
        FROM (
            SELECT
                TRIM(xbtld.BARCODE_NUMBER) AS BARCODE_NUMBER,
                xbtld.TRANSACTION_TYPE_NAME,
                xbtld.ORGANIZATION_NAME,
                xbtld.BARCODE_STATUS,
                xbtld.TRANSACTION_STATUS,
                xbtld.LAST_UPDATE_DATE,
                ROW_NUMBER() OVER (
                    PARTITION BY TRIM(xbtld.BARCODE_NUMBER)
                    ORDER BY xbtld.LAST_UPDATE_DATE DESC NULLS LAST,
                             xbtld.TRANSACTION_ID DESC NULLS LAST
                ) rn
            FROM wksp_xxcl.XXCL_BARCODE_TRX_LOC_DETAILS xbtld
            WHERE xbtld.BARCODE_NUMBER IS NOT NULL
              AND TRIM(xbtld.BARCODE_NUMBER) IN ({placeholders})
        ) ranked
        WHERE ranked.rn = 1
    """
    out: dict[str, dict] = {}
    for row in _fetch_by_barcode_chunks(sql, barcodes):
        bc = str(row.get("BARCODE_NUMBER") or "").strip()
        if bc:
            out[bc] = {
                "organization_name": row.get("ORGANIZATION_NAME"),
                "transaction_type_name": row.get("TRANSACTION_TYPE_NAME"),
                "barcode_status": row.get("BARCODE_STATUS"),
                "transaction_status": row.get("TRANSACTION_STATUS"),
                "last_update_date": row.get("LAST_UPDATE_DATE"),
            }
    return out


def fetch_duplicate_onhand_by_barcodes(barcodes: list[str]) -> dict[str, int]:
    """FG lot_numbers with duplicate on-hand rows in Fusion."""
    sql = """
        SELECT
            xioqd.lot_number,
            COUNT(*) AS duplicate_count
        FROM wksp_xxcl.XXCL_INV_ONHAND_QUANTITIES_DETAIL xioqd
        INNER JOIN wksp_xxcl.xxcl_item_master xm
            ON xm.inventory_item_id = xioqd.inventory_item_id
           AND xm.item_class_name LIKE 'FG%%'
        WHERE TRIM(xioqd.lot_number) IN ({placeholders})
        GROUP BY xioqd.lot_number
        HAVING COUNT(*) > 1
    """
    out: dict[str, int] = {}
    for row in _fetch_by_barcode_chunks(sql, barcodes):
        bc = str(row.get("LOT_NUMBER") or "").strip()
        if bc:
            try:
                out[bc] = int(row.get("DUPLICATE_COUNT") or 0)
            except (TypeError, ValueError):
                out[bc] = 2
    return out


def fetch_sold_onhand_by_barcodes(barcodes: list[str]) -> dict[str, dict]:
    """Sold barcodes that still have on-hand quantity in Fusion."""
    sql = """
        SELECT
            xbtld.barcode_number,
            xbtld.barcode_status,
            xbtld.transaction_status,
            xioqd.lot_number,
            xioqd.organization_id
        FROM (
            SELECT
                TRIM(barcode_number) AS barcode_number,
                barcode_status,
                transaction_status,
                ROW_NUMBER() OVER (
                    PARTITION BY TRIM(barcode_number)
                    ORDER BY creation_date DESC
                ) rn
            FROM wksp_xxcl.xxcl_barcode_trx_loc_details
            WHERE barcode_number IS NOT NULL
              AND TRIM(barcode_number) IN ({placeholders})
        ) xbtld
        INNER JOIN wksp_xxcl.xxcl_inv_onhand_quantities_detail xioqd
            ON xbtld.barcode_number = TRIM(xioqd.lot_number)
        WHERE xbtld.rn = 1
          AND xbtld.barcode_status = 'SOLD'
          AND xbtld.transaction_status = 'SOLD'
    """
    out: dict[str, dict] = {}
    for row in _fetch_by_barcode_chunks(sql, barcodes):
        bc = str(row.get("BARCODE_NUMBER") or row.get("LOT_NUMBER") or "").strip()
        if bc:
            out[bc] = {
                "barcode_status": row.get("BARCODE_STATUS"),
                "transaction_status": row.get("TRANSACTION_STATUS"),
                "organization_id": row.get("ORGANIZATION_ID"),
            }
    return out


def fetch_work_orders_by_sales_order(sales_order_no: str) -> list[dict]:
    """Work orders + GRN/PO/ASBN for a sales order (hash_id)."""
    sales_order_no = (sales_order_no or "").strip()
    if not sales_order_no or not fusion_db_configured():
        return []

    sql = """
        SELECT
            xwwob.WORK_ORDER_NUMBER,
            CASE
                WHEN xwwob.WORK_ORDER_NUMBER LIKE '%%/%%/%%/%%'
                    THEN 'INHOUSE'
                WHEN xwwob.WORK_ORDER_NUMBER LIKE 'MUMFC-%%'
                    THEN 'JOB WORK'
                ELSE 'INHOUSE'
            END AS MANUFACTURING_TYPE,
            xwwob.ATTRIBUTE_CHAR1 AS SALES_ORDER,
            xwwob.ATTRIBUTE_CHAR5 AS PO_NUMBER,
            xvag.GRN_STATUS,
            xvag.ASBN_NUMBER,
            xvag.INVOICE_NUMBER,
            xvag.GRN_number,
            xvag.huid_number,
            xwwob.PLANNED_START_DATE,
            xwwob.ACTUAL_START_DATE,
            xwwob.PLANNED_COMPLETION_DATE,
            xwwob.COMPL_SUBINVENTORY_CODE,
            xvag.GRN_CREATED_DATE
        FROM wksp_xxcl.XXCL_WIE_WORK_ORDERS_B xwwob
        LEFT JOIN (
            SELECT *
            FROM (
                SELECT
                    xvag.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY xvag.SALES_ORDER_NO
                        ORDER BY xvag.GRN_CREATED_DATE DESC
                    ) rn
                FROM wksp_xxcl.XXCL_VNDR_ADD_GRN xvag
            )
            WHERE rn = 1
        ) xvag
            ON xvag.SALES_ORDER_NO = xwwob.ATTRIBUTE_CHAR1
        WHERE xwwob.ATTRIBUTE_CHAR1 = :sales_order_no
        ORDER BY xwwob.WORK_ORDER_NUMBER
    """
    try:
        rows = fetch_all(sql, {"sales_order_no": sales_order_no})
        return [
            {
                "work_order_number": row.get("WORK_ORDER_NUMBER"),
                "manufacturing_type": row.get("MANUFACTURING_TYPE"),
                "sales_order": row.get("SALES_ORDER"),
                "po_number": row.get("PO_NUMBER"),
                "grn_status": row.get("GRN_STATUS"),
                "asbn_number": row.get("ASBN_NUMBER"),
                "invoice_number": row.get("INVOICE_NUMBER"),
                "grn_number": row.get("GRN_NUMBER"),
                "huid_number": row.get("HUID_NUMBER"),
                "planned_start_date": row.get("PLANNED_START_DATE"),
                "actual_start_date": row.get("ACTUAL_START_DATE"),
                "planned_completion_date": row.get("PLANNED_COMPLETION_DATE"),
                "compl_subinventory_code": row.get("COMPL_SUBINVENTORY_CODE"),
                "grn_created_date": row.get("GRN_CREATED_DATE"),
            }
            for row in rows
        ]
    except Exception:
        return []
