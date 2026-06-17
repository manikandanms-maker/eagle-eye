# 🦅 Eagle Eye — Order Audit & First-Check Validation (CaratLane)

> **Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.**

Eagle Eye validates every CaratLane order against Magento, Oracle Fusion (PaaS + SaaS), and internal business rules **before** it ships. Paste an order number (hash, increment_id, or entity_id) → get a pass/fail checklist with CL vs PaaS vs SaaS barcode sync, ATP/WD/UOM, manufacturing, finance, and SOP escalation routing. Optionally run the full revenue-leakage audit (R01–R17) with AI narration.

---

## Table of contents

1. [What it does today](#1-what-it-does-today)
2. [Architecture](#2-architecture)
3. [Data sources & integrations](#3-data-sources--integrations)
4. [First-check validation (pre-audit)](#4-first-check-validation-pre-audit)
5. [SOP escalation checks](#5-sop-escalation-checks)
6. [Full audit engine (R01–R17)](#6-full-audit-engine-r01r17)
7. [Fusion BI Publisher (SaaS SOAP)](#7-fusion-bi-publisher-saas-soap)
8. [Web UI](#8-web-ui)
9. [Google Chat notifications](#9-google-chat-notifications)
10. [API reference](#10-api-reference)
11. [Configuration](#11-configuration)
12. [Run locally](#12-run-locally)
13. [Project structure](#13-project-structure)
14. [Documentation](#14-documentation)

---

## 1. What it does today

Eagle Eye operates in **two steps**:

| Step | Action | Data source | Output |
|------|--------|-------------|--------|
| **1. Validate** | First-check against live Magento + Fusion | Magento RR, Fusion DB, Fusion SOAP | Checklist (pass/fail/warn), enriched order lines, work orders, integrations status |
| **2. Run Audit** | Revenue-leakage rule engine + optional Claude | Fixtures or live loader | PASS/FLAG verdict, ₹ at risk, findings, AI narrative |

**Validate** is the primary live path — it connects to production read replicas and Fusion APIs. **Audit** still supports offline fixtures for demo and can be wired to live OIC when available.

Hybrid Safety principle (unchanged):

- **Code decides the money** — deterministic Python checks are the source of truth.
- **Claude only explains** — optional narration; offline fallback if no API key.

---

## 2. Architecture

```
Order number (hash / increment_id / entity_id)
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│  validate_order()  — audit/magento.py                         │
│  • Magento: order header + barcode lines                      │
│  • Enrich: Fusion DB + Fusion SOAP + Vendor QC App             │
│  • Pre-audit rules (14) + SOP checks (15)                     │
│  • Google Chat webhook (optional)                             │
└───────────────────────────────────────────────────────────────┘
        │
        ▼
   Web UI checklist + order detail table + work orders
        │
        ▼ (if all first-checks pass)
┌───────────────────────────────────────────────────────────────┐
│  audit()  — audit/engine.py                                   │
│  • loader.py → order snapshot                                 │
│  • rules.py  → R01–R17 deterministic checks                   │
│  • reasoner.py → Claude narrative (optional)                  │
└───────────────────────────────────────────────────────────────┘
```

Key modules:

| Module | Role |
|--------|------|
| `audit/magento.py` | Order lookup, enrichment, first-check + SOP validation |
| `audit/db.py` | Magento MySQL read-replica connection |
| `audit/fusion_db.py` | Oracle Fusion ATP (PaaS) read-only queries |
| `audit/fusion_report.py` | Fusion BI Publisher SOAP — SaaS reports (ATP, barcode, finance) |
| `audit/sop_checks.py` | 15 SOP escalation checks with team routing |
| `audit/google_chat.py` | Google Chat webhook on every validation |
| `audit/engine.py` | Full audit rule runner |
| `web/app.js` | Compact checklist UI, filters, order detail table |

---

## 3. Data sources & integrations

### 3.1 Magento read-replica (`audit/db.py`)

**Env:** `MAGENTO_DB_*`

Used for:

- Order lookup by `increment_id`, `entity_id`, `hash_id`, or ERP order id
- Order lines (`sales_flat_order_item_qty`) — barcodes, SKUs, EDD min/max, price breakup, ERP status
- Customer email, PAN, Fusion party number
- Invoice mapping per barcode (`caratlane_invoices` + JSON barcode extraction)
- **Vendor QC App** — `indus_purchase_orders_item_qty` (latest QC status per barcode + PO)
- CL barcode attributes — `erp_barcode_attributes` + `erp_location_master_dtl`

### 3.2 Oracle Fusion PaaS DB (`audit/fusion_db.py`)

**Env:** `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, `ORACLE_WALLET_DIR`

Read-only wallet connection to `WKSP_XXCL` schema. Used for:

| Function | Table / view | Purpose |
|----------|--------------|---------|
| `fetch_manufacturing_by_skus` | `XXCL_ITEM_STRUCTURE_DTL_V`, item master, supplier views | Manufacturing type: **Inhouse**, **JW**, **Outright** |
| `fetch_inhouse_bag_by_order` | Inhouse bag / metal-loss tables | Bag status, factory, loss weight, loss stock value |
| `fetch_jw_grn_by_order` | `xxcl_vndr_add_grn` | JW GRN status, gross weight, PO (`TRANSACTION_NUMBER`) |
| `fetch_paas_barcode_trx_by_barcodes` | `XXCL_BARCODE_TRX_LOC_DETAILS` | Latest PaaS location, transaction type, barcode/transaction status |
| `fetch_duplicate_onhand_by_barcodes` | `XXCL_INV_ONHAND_QUANTITIES_DETAIL` | Duplicate on-hand rows per lot_number |
| `fetch_sold_onhand_by_barcodes` | Barcode trx + on-hand join | Sold barcodes still showing on-hand qty |
| `fetch_work_orders_by_sales_order` | `XXCL_WIE_WORK_ORDERS_B` + `XXCL_VNDR_ADD_GRN` | Work orders, PO, ASBN, GRN, planned dates, AP invoice number |

### 3.3 Fusion BI Publisher SOAP — SaaS (`audit/fusion_report.py`)

**Env:** `FUSION_SOAP_USER`, `FUSION_SOAP_PASSWORD`, report paths and params (see [§7](#7-fusion-bi-publisher-saas-soap))

Synchronous `runReport` calls to Oracle Cloud BI Publisher. Used for:

- **Item ATP / Work Definition / UOM** — per SKU via `item_number` param
- **Barcode location (SaaS)** — per barcode via `lot_number` + `barcode` params
- **Barcode transaction type (SaaS)** — per barcode via `lot_number` param; full name mapped to code (e.g. `Purchase Order Receipt` → `POR`)
- **AR invoice status** — by dispatched invoice number
- **AP invoice payment / ledger** — by work-order invoice number
- **Work order RM consumption** — by work order number

Results are cached in memory (TTL configurable) and optionally on disk for the bulk ATP report.

### 3.4 Vendor QC App (Magento)

**Query:** `indus_purchase_orders_item_qty` with `JSON_TABLE` over `status_update_time_stamp`

For **JW (Job Work)** manufacturing lines:

1. Collect PO numbers from Fusion GRN (`TRANSACTION_NUMBER`) and work orders (`PO_NUMBER` / `ATTRIBUTE_CHAR5`)
2. Look up latest QC status per `(barcode, po_number)`
3. **Fallback:** lookup by barcode alone if PO match fails

Fields exposed on order lines: `po_number`, `vendor_qc_status`, `vendor_qc_status_time`, `grn_status`, `grn_gross_weight`

### 3.5 Google Chat (`audit/google_chat.py`)

**Env:** `GOOGLE_CHAT_WEBHOOK_URL`

Posts a summary on **every** validation search (pass or fail): order id, customer, pass/fail counts, failed checks with actions, warnings, and **high-value order alert** when `net_payable` > ₹5,00,000.

### 3.6 Audit fixtures (offline demo)

**Path:** `data/orders/*.json`

Used when `MAGENTO_DB_PASSWORD` is not set. Five fixtures: 1 PASS + 4 leakage scenarios. See [§6](#6-full-audit-engine-r01r17).

---

## 4. First-check validation (pre-audit)

Triggered by **Validate** in the UI or `GET /api/validate/{order_number}`.

Enrichment runs first (`_enrich_lines`), then **14 pre-audit rules** grouped by module:

### ORDER checks

| Check | What it validates | Source |
|-------|-------------------|--------|
| Fusion order | `is_fusion_order = 1` | Magento `sales_flat_order` |
| Pushed to ERP | `is_pushed = 1` | Magento header |
| Non-test customer email | Email does not contain `test` | Magento customer / shipping |
| Barcode on line items | Every qty line has a barcode | Order lines |
| ERP status eligible | Status ∈ {On Hold, Processing, Dispatched, Cancelled} | Line ERP status |
| Expected delivery dates | Both `expected_delivery_date_min` and `_max` present | Order lines |

### CUSTOMER checks

| Check | What it validates |
|-------|-------------------|
| Customer name present | Non-empty first + last name |
| Customer email valid | Well-formed email |
| Fusion party number | Non-empty `fusion_party_number` |

### BARCODE checks (CL vs PaaS vs SaaS)

| Check | What it validates | Systems compared |
|-------|-------------------|------------------|
| Barcode location sync | Location aligned | CL `erp_barcode_attributes`, PaaS `XXCL_BARCODE_TRX_LOC_DETAILS`, SaaS location BI report |
| Barcode transaction sync | Transaction type + statuses aligned | Same three sources; transaction codes normalized via `TRANSACTION_TYPE_NAME_TO_CODE` (60+ mappings, e.g. SOI ↔ Sales Order Issue) |
| No duplicate on-hand | No duplicate FG on-hand rows | Fusion `XXCL_INV_ONHAND_QUANTITIES_DETAIL` |
| Sold barcodes not on-hand | SOLD barcodes have no on-hand qty | Fusion barcode trx + on-hand |

### PRICING checks

| Check | What it validates |
|-------|-------------------|
| Barcode price breakup | Component sum = sub_total (±10), sub_total + tax = selling (±3), 3% GST, diamond/gemstone FV rules | Magento `sfoi.price_breakup` (pricing_engine rules) |

Bulk JSON fields (`price_breakup`, `tax_breakup`, `discount_breakups`) are stripped from the API response via `_slim_line()` to keep the UI fast.

---

## 5. SOP escalation checks

After pre-audit rules, **15 SOP checks** run (`audit/sop_checks.py`). Each failed check includes an **action** string naming the escalation team.

| # | Check | Escalation team | Data source |
|---|-------|-----------------|-------------|
| 1 | PAN on order | Service team | Magento PAN on lines |
| 2 | Order not processing > 3 months | Service / ERP | `created_at` vs today if still Processing |
| 3 | Manufacturing barcode reserved in Fusion | Service / Buy | `barcode_reserved_in_fusion` |
| 4 | MTO order has barcode | ERP / Service | MTO lines without barcode |
| 5 | PO generated (JW / Buy) | Buy team | Work order `PO_NUMBER` for JOB WORK |
| 6 | ATP / WD / UOM present | ERP team | Fusion SOAP item report (PRESENT) |
| 7 | Inhouse metal loss values | Manufacturing | Inhouse bag loss weight / stock value |
| 8 | QC / GRN status | Service / Buy | Vendor QC App + Fusion GRN |
| 9 | Work order RM consumption | Manufacturing / ERP | SaaS WO RM consumption report |
| 10 | Work order completion (Buy) | Buy / Manufacturing | Planned completion date vs today |
| 11 | AP invoice payment status | Finance / Buy | SaaS AP invoice status report |
| 12 | AP ledger ID present | Finance / ERP | SaaS AP report `LEDGER_ID` |
| 13 | AR invoice status | Finance / ERP | SaaS AR invoices report |
| 14 | Profile balance & Transfer (partial ILO) | — | Placeholder (not implemented) |
| 15 | High-value order flag (> ₹5L) | Finance review | `net_payable`; passes but warns |

Checks with `action` set on a **passed** check appear as **warnings** in the UI (e.g. high-value orders).

---

## 6. Full audit engine (R01–R17)

Available via **Run Audit** after first-check passes, or `GET /api/audit/{order_id}`.

Deterministic rules in `audit/rules.py`. Each returns severity, ₹ impact, expected vs actual.

| # | Module | Rule | What it catches |
|---|--------|------|-----------------|
| R01 | PRICING | Item price == barcode computed price | Mispriced barcode → underbilling |
| R02 | PRICING | `price_before_tax == price − discount` | Discount math error |
| R03 | PRICING | `amount == price_before_tax + tax` | Item total wrong |
| R04 | PRICING | Tax ≈ price_before_tax × GST rate | Wrong/zero tax |
| R05 | PRICING | Header sub_total == Σ line price_before_tax | Header/line mismatch |
| R06 | PRICING | Header grand_total == sub_total + tax | Grand total wrong |
| R07 | DISCOUNT | Discount percent ≤ cap by order type | Over-discount |
| R08 | DISCOUNT | Item + order discount stack ≤ combined cap | Double-discount |
| R09 | COUPON | Coupon code ⇔ discount amount | Coupon leakage |
| R10 | INVOICE | Invoice present for shippable status | Missing invoice push |
| R11 | DELIVERY | EDD present and not before order date | Bad delivery date |
| R12 | FULFILLMENT/ITR | ORDER DETAIL vs FULFILLMENT DETAIL match | Sub-inventory sync mismatch |
| R13 | ITEM | Diamond ⇒ certificate present | Missing cert |
| R14 | CUSTOMER | Customer id / name / email valid | Bad customer data |
| R15 | ADDRESS | Ship pincode (6 digits) + state | Undeliverable address |
| R16 | RESTRICTION | Order-type policy (online payment, old gold, high-value approval) | Policy bypass |
| R17 | PRICING | Price > 0, barcode/SKU present | Zero-priced / barcodeless line |

Thresholds: `audit/config.py` (GST rate, discount caps, ₹ tolerance).

### Fixtures

| File | Type | Expected | Issue |
|------|------|----------|-------|
| `order_pass.json` | JM | PASS | None |
| `order_price_leak.json` | EZ | FLAG | Underpriced barcode (R01) |
| `order_discount_abuse.json` | online | FLAG | 35% discount vs 15% cap (R07/R09) |
| `order_invoice_missing.json` | JR | FLAG | No invoice, EDD in past (R10/R11) |
| `order_itr_mismatch.json` | oldgold | FLAG | ITR mismatch + missing old-gold value (R12/R16) |

---

## 7. Fusion BI Publisher (SaaS SOAP)

All reports use SOAP 1.2 to `ExternalReportWSSService` with credentials in the envelope.

### Item ATP / WD / UOM

| Setting | Default |
|---------|---------|
| Report | `/Custom/Extraction Reports/ITEM ATP WD UOM STATUS/item_wd_atp_uom.xdo` |
| Param | `item_number` |
| Lookup | Per-item SOAP on cache miss (`FUSION_REPORT_SYNC_ON_MISS=1`) or bulk CSV cache |

Returns: `atp_status`, `wd_status`, `uom_status` (typically `PRESENT` / `MISSING`).

### Barcode location (SaaS)

| Setting | Default |
|---------|---------|
| Report | `.../saas_location_barcode_p.xdo` |
| Params | `lot_number` + `barcode` (same value) |
| Enable | `FUSION_ENABLE_BARCODE_SAAS_REPORTS=1` |

### Barcode transaction (SaaS)

| Setting | Default |
|---------|---------|
| Report | `.../Barcode_transaction_type_4_RPT.xdo` |
| Param | `lot_number` |
| Mapping | Full transaction name → code via `TRANSACTION_TYPE_NAME_TO_CODE` |

### Finance reports (SaaS)

| Report | Param | Fields used |
|--------|-------|-------------|
| AR invoices | `invoice_number` | Status, amount due remaining |
| AP invoice status | `invoice_number` | Payment status, ledger ID, balance |
| WO RM consumption | `work_order_number` | Row count → consumed flag |

Enable: `FUSION_ENABLE_SAAS_FINANCE_REPORTS=1`

### Cache refresh

```bash
# Bulk ATP/WD/UOM cache (scheduled report, may take minutes)
python -m audit.fusion_report --refresh

# Test barcode SaaS reports for one lot
python -m audit.fusion_report --barcode JL05717-2YP500-17

# API
POST /api/fusion-report/refresh
GET  /api/fusion-report/status
```

---

## 8. Web UI

**URL:** `http://127.0.0.1:8000` (served by `server.py`)

### Validate flow

1. Enter order number → **Validate**
2. **Summary bar** — Passed / Failed / Warning counts, progress bar, order value (HIGH VALUE badge if > ₹5L)
3. **Filter buttons** — All · Issues only · Failed · Warnings · Passed
4. **Grouped checklist** — Order / Customer / Barcode / Pricing / SOP sections with `(x/y passed)`; failed sections expanded, passed collapsed
5. **Compact check cards** — Title + badge; details behind “Show details”
6. **Customer card** — Name, email, PAN, Fusion party, integration chips
7. **Order details table** — All enriched fields per barcode line
8. **Work orders table** — Fusion WOs with AP payment, ledger ID, RM consumption, completion status

### Order detail columns (live data)

**Always shown:** barcode, SKU, ERP status, EDD min/max, ATP/WD/UOM, invoice fields, manufacturing type, CL/PaaS/SaaS location & transaction, duplicate on-hand count, sold-on-hand flag, pricing.

**JW lines only:** PO number, GRN, vendor QC status + timestamp.

**Inhouse lines only:** bag status, factory, loss weight, loss stock value.

**Invoiced lines:** AR status, amount due remaining.

**Work orders:** WO number, mfg type, PO, ASBN, GRN, planned dates, completion status, AP payment, ledger ID, RM consumed.

Manufacturing-specific columns show **N/A** on rows where they do not apply.

### Run Audit

Enabled only when all first-check validations pass. Shows verdict card, ₹ at risk, findings table, optional Claude narrative.

---

## 9. Google Chat notifications

Set `GOOGLE_CHAT_WEBHOOK_URL` in `.env`.

Every validation posts:

- PASS / FAIL verdict
- Order hash, entity_id, customer
- Order value; **HIGH VALUE ORDER (> ₹5L)** when applicable
- Failed checks with escalation actions
- Warning checks (e.g. high-value finance review)

Sent asynchronously so validation is not blocked.

---

## 10. API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/api/health` | Magento / Fusion / SOAP / webhook / cache status |
| `GET` | `/api/orders` | List order IDs (fixtures or live) |
| `GET` | `/api/validate/{order_number}` | **First-check validation** — full checklist + enriched lines + work orders |
| `GET` | `/api/audit/{order_id}` | Full R01–R17 audit + AI narrative |
| `GET` | `/api/fusion-report/status` | ATP cache status |
| `POST` | `/api/fusion-report/refresh` | Refresh bulk ATP/WD/UOM CSV cache |

### Validate response shape (abbreviated)

```json
{
  "order_number": "EZTRCKBR789CP-JM",
  "found": true,
  "valid": false,
  "hash_id": "EZTRCKBR789CP-JM",
  "entity_id": 12345,
  "line_count": 4,
  "checks": [{ "label": "...", "module": "ORDER", "passed": true, "actual": "...", "action": "" }],
  "customer": { "name": "...", "email": "...", "pan_no": "..." },
  "header": { "net_payable": 920388, "is_fusion_order": 1 },
  "lines": [{ "barcode": "...", "atp_status": "PRESENT", "saas_location_name": "...", "vendor_qc_status": "COMPLETED" }],
  "work_orders": [{ "work_order_number": "...", "ap_payment_status": "Unpaid", "ap_ledger_id": "..." }],
  "integrations": { "magento_db": true, "fusion_db": true, "item_atp_wd_uom": "run_report", "saas_finance": true }
}
```

---

## 11. Configuration

Copy `.env.example` → `.env` and fill in credentials.

### Required for live validation

```bash
MAGENTO_DB_PASSWORD=...
ORACLE_PASSWORD=...
ORACLE_WALLET_DIR=/path/to/wallet
FUSION_SOAP_USER=...
FUSION_SOAP_PASSWORD=...
```

### Optional

| Variable | Purpose | Default |
|----------|---------|---------|
| `FUSION_ENABLE_BARCODE_SAAS_REPORTS` | SaaS location/transaction per barcode | `1` |
| `FUSION_ENABLE_SAAS_FINANCE_REPORTS` | AR/AP/WO RM SaaS reports | `1` |
| `FUSION_REPORT_SYNC_ON_MISS` | Per-item ATP SOAP on cache miss | `1` |
| `FUSION_REPORT_CACHE_TTL` | In-memory report cache seconds | `21600` (6h) |
| `GOOGLE_CHAT_WEBHOOK_URL` | Validation notifications | disabled |
| `ANTHROPIC_API_KEY` | Claude audit narration | offline fallback |

See `.env.example` for all Fusion report paths and parameter names.

---

## 12. Run locally

```bash
cd eagle-eye
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill credentials

python server.py       # → http://127.0.0.1:8000
```

**CLI audit (fixtures, no DB):**

```bash
python cli.py CL-ORD-1002
```

**Streamlit UI (alternative):**

```bash
streamlit run app.py
```

**Health check:**

```bash
curl http://127.0.0.1:8000/api/health | jq
```

After UI changes, hard-refresh the browser (`Cmd+Shift+R`) to bust the static cache (`app.js?v=…`).

---

## 13. Project structure

```
eagle-eye/
├── README.md
├── server.py                 # FastAPI — validate + audit + static UI
├── cli.py                    # Fixture-based CLI audit
├── app.py                    # Streamlit alternative UI
├── .env.example
├── requirements.txt
├── web/
│   ├── index.html
│   ├── style.css
│   └── app.js                # Checklist UI, filters, order detail table
├── audit/
│   ├── magento.py            # Order lookup, enrichment, first-check validation
│   ├── sop_checks.py         # 15 SOP escalation checks
│   ├── fusion_db.py          # Oracle Fusion PaaS read-only queries
│   ├── fusion_report.py      # Fusion BI Publisher SOAP + transaction map
│   ├── google_chat.py        # Google Chat webhook
│   ├── db.py                 # Magento MySQL connection
│   ├── loader.py             # Fixture / OIC order loader
│   ├── engine.py             # R01–R17 audit runner
│   ├── rules.py              # Rule catalog
│   ├── reasoner.py           # Claude narration + offline fallback
│   ├── schema.py             # Normalized order model
│   └── config.py             # GST, discount caps, tolerances
├── data/
│   ├── orders/               # Offline audit fixtures
│   └── fusion_reports/       # Cached ATP/WD/UOM CSV (generated)
├── docs/                     # HLD, LLD, requirements, project plan
└── tests/
```

---

## 14. Documentation

| Doc | Purpose |
|-----|---------|
| [docs/PRESENTATION.md](docs/PRESENTATION.md) | **Panel presentation** — demo script, Q&A, impact, roadmap |
| [docs/HLD.md](docs/HLD.md) | High-level design — context, architecture, deployment |
| [docs/LLD.md](docs/LLD.md) | Low-level design — data model, API contract, extension recipes |
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Scope, deliverables, demo script, roadmap |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Functional requirements + production gaps |

---

## Integration status summary

| Integration | Status | Module |
|-------------|--------|--------|
| Magento order lookup | ✅ Live | `db.py`, `magento.py` |
| Magento invoice + Vendor QC App | ✅ Live | `magento.py` |
| CL barcode attributes | ✅ Live | `magento.py` |
| Fusion PaaS — manufacturing type | ✅ Live | `fusion_db.py` |
| Fusion PaaS — inhouse bag / metal loss | ✅ Live | `fusion_db.py` |
| Fusion PaaS — JW GRN | ✅ Live | `fusion_db.py` |
| Fusion PaaS — barcode trx/loc | ✅ Live | `fusion_db.py` |
| Fusion PaaS — duplicate / sold on-hand | ✅ Live | `fusion_db.py` |
| Fusion PaaS — work orders | ✅ Live | `fusion_db.py` |
| Fusion SaaS — ATP/WD/UOM per item | ✅ Live | `fusion_report.py` |
| Fusion SaaS — barcode location | ✅ Live | `fusion_report.py` |
| Fusion SaaS — barcode transaction | ✅ Live | `fusion_report.py` |
| Fusion SaaS — AR / AP / WO RM finance | ✅ Live | `fusion_report.py` |
| Barcode price breakup validation | ✅ Live | `magento.py` |
| SOP escalation checks (14 of 15) | ✅ Live | `sop_checks.py` |
| Google Chat webhook | ✅ Live | `google_chat.py` |
| Full audit R01–R17 | ✅ Fixtures + engine | `engine.py`, `rules.py` |
| Claude AI narration | ✅ Optional | `reasoner.py` |
| ILO profile balance transfer | ⏳ Placeholder | `sop_checks.py` |
| Live OIC order fetch for audit | ⏳ Stub | `loader.py` |

---

*Eagle Eye — Hybrid Safety: code decides the money, Claude only explains.*
