# 🦅 Eagle Eye — AI-Powered Order Audit (CaratLane Hackathon)

> **Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.**
>
> Paste an **Order ID** → in seconds an AI auditor runs every CaratLane module check
> and returns **PASS / FLAG** with the **₹ value at risk** and a plain-English reason.
> Designed to fire the moment an order is placed and catch **revenue leakage** before it ships.

---

## 1. The problem (pitch this in one line)

Orders flow Store/Online → OIC (SaaS) → PaaS sync → ERP (Oracle Fusion) → back to PaaS.
Across that chain, money quietly leaks: a barcode priced wrong, an over-applied discount,
a coupon that wasn't authorised, a missing invoice push, an EDD in the past, a sub-inventory
transfer (ITR) that never reconciled. Traditional SOC2-style audits are periodic and generic.
**We audit every order, instantly, against CaratLane's actual business rules.**

## 2. The approach — "Hybrid Safety" (steal this from the references)

The tools4ai / agentic-rules-engine articles are **conceptually right**:
- **LLM interprets & explains. Code decides.**
- We **never** let the LLM compute whether ₹ amounts reconcile (it will hallucinate and
  approve a leaking order). Deterministic Python does the math; **Claude** classifies
  severity, writes the auditor narrative, and catches anomalies no rule anticipated.

```
Order ID
   │
   ▼
[1] COLLECT  loader.py        → fetch order snapshot (fixture now, OIC API later)
   │
   ▼
[2] DECIDE   rules.py/engine  → deterministic checks, structured findings + ₹ impact   ← source of truth
   │
   ▼
[3] EXPLAIN  reasoner.py      → Claude: severity, root cause, fix, anomaly catch-all   ← optional, has offline fallback
   │
   ▼
[4] VERDICT  app.py / cli.py  → PASS / FLAG card, ₹ at risk, reasons, suggested fix
```

## Documentation

| Doc | What's inside |
|---|---|
| [docs/HLD.md](docs/HLD.md) | High-Level Design — context, architecture, modules, data flow, deployment, risks |
| [docs/LLD.md](docs/LLD.md) | Low-Level Design — data model, full rule catalog, API contract, error handling, extension recipes |
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Finalized plan — scope, deliverables status, 24h timeline, roles, demo script, roadmap |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Functional/non-functional requirements + **missing requirements & dependencies** for production |

## 3. Project structure

```
Hackathon/
├── README.md                  ← this file (plan + rules + demo script)
├── docs/                      ← design & planning docs (see "Documentation" below)
├── requirements.txt
├── .env.example               ← ANTHROPIC_API_KEY (optional; demo works without it)
├── server.py                  ← FastAPI backend + serves the SaaS web UI
├── web/                       ← dark-theme SaaS frontend (index.html, style.css, app.js)
├── cli.py                     ← zero-dependency demo:  python cli.py CL-ORD-1001
├── app.py                     ← Streamlit verdict UI (alternative)
├── audit/
│   ├── loader.py              ← DATA COLLECTION: fixture loader + OIC fetch stub + normalizer
│   ├── schema.py              ← normalized order model + helpers (safe float parsing)
│   ├── rules.py               ← the rule catalog (one function per check)
│   ├── engine.py              ← runs rules, aggregates findings, computes verdict + ₹ at risk
│   ├── reasoner.py            ← Claude reasoner with graceful offline fallback
│   └── config.py             ← tunable policy thresholds (GST rate, discount caps, tolerances)
├── data/orders/              ← fixtures: 1 clean PASS + broken cases (EZ/JM/JR/online/oldgold)
└── tests/                    ← quick sanity tests for the rule engine
```

## 4. Data collection — how we get the order

The real order shape is already in this monorepo:
`erp_data_sync/validate_order_json2.yml` (order_header + order_items[ORDER DETAIL / FULFILLMENT DETAIL] + addresses).
Our fixtures mirror those exact field names, so swapping fixtures for live data is a one-function change.

**Three sources, same normalizer (`loader.py`):**
1. **Fixtures (demo default)** — `data/orders/*.json`. Zero cost, always works on stage.
2. **OIC / Fusion REST (stub provided)** — `fetch_from_oic(order_id)`. Drop in the real
   endpoint + auth header when available; everything downstream is unchanged.
3. **Webhook trigger (prod vision)** — `oneview_webhook` already receives orders; Eagle Eye
   subscribes to the "order placed" event and audits automatically. For the demo we simulate
   the trigger with the order-ID box / a button.

Source-of-truth pricing comes from the barcode BOM — see CaratLane's own
`pricing_engine` and `Fusion OIC Integration/XXCL_BARCODE_COMP_PRICING_VIEW_*.sql`.
Each fixture carries a `pricing_reference` block (metal + making + stone = computed price)
so the audit can reconcile the order's price against what the barcode *should* cost.

## 5. Rules catalog (the moat)

Each rule returns: `module, rule_id, severity, status, title, expected, actual, rupee_impact, detail`.
Severity: CRITICAL / HIGH / MEDIUM / LOW. A single CRITICAL or HIGH ⇒ order is **FLAGGED**.

| # | Module | Rule | What it catches (leakage) |
|---|--------|------|----------------------------|
| R01 | PRICING | item price == barcode `pricing_reference.computed_price` | mispriced barcode/SKU → underbilling |
| R02 | PRICING | `price_before_tax == price − discount` | discount math doesn't tie out |
| R03 | PRICING | `amount == price_before_tax + tax` | item total wrong |
| R04 | PRICING | `tax ≈ price_before_tax × GST_RATE` | wrong/zero tax → compliance + leakage |
| R05 | PRICING | header `sub_total == Σ item price_before_tax` | header/line mismatch |
| R06 | PRICING | header `grand_total == sub_total + tax` | grand total wrong |
| R07 | DISCOUNT | `discount_percent ≤ cap[order_type]` | unauthorised over-discount |
| R08 | DISCOUNT | item + order discount stack ≤ combined cap | double-discount leakage |
| R09 | COUPON | coupon_code present ⇒ discount_amount > 0 (and vice-versa) | coupon applied with no value / value with no coupon |
| R10 | INVOICE | fulfillment `invoice_no` present for shippable status | **invoice push missing** → can't bill |
| R11 | DELIVERY | `edd_date` present and not before `order_date` | impossible/blank delivery date |
| R12 | FULFILLMENT/ITR | ORDER DETAIL vs FULFILLMENT DETAIL: barcode/price/amount match | sub-inventory transfer/sync mismatch |
| R13 | ITEM | diamond item (`is_diamond`) ⇒ `certificate_no` present | missing cert on diamond |
| R14 | CUSTOMER | customer_id / name / email present & email well-formed | bad customer master data |
| R15 | ADDRESS | ship pincode present, 6 digits, ship state present | undeliverable / tax-jurisdiction risk |
| R16 | RESTRICTION | order_type-specific (ONLINE needs payment_mode; OLDGOLD needs old-gold value; high-value needs financial_approval=YES) | policy bypass |
| R17 | PRICING | item `price > 0` and barcode/sku present & well-formed | zero-priced / barcodeless line |

Thresholds live in `audit/config.py` (GST_RATE, DISCOUNT_CAP per order type, money tolerance ₹1).

## 6. Test cases / fixtures

| File | Order type | Expected | Injected issue |
|------|-----------|----------|----------------|
| `order_pass.json` | JM store | PASS | none — everything reconciles |
| `order_price_leak.json` | EZ | FLAG | item price ₹924 below barcode computed price (R01) |
| `order_discount_abuse.json` | online | FLAG | 35% discount vs 15% cap + coupon with ₹0 value (R07/R09) |
| `order_invoice_missing.json` | JR | FLAG | shippable item, no invoice_no, EDD in past (R10/R11) |
| `order_itr_mismatch.json` | oldgold | FLAG | DETAIL vs FULFILLMENT barcode/price mismatch + missing old-gold value (R12/R16) |

## 7. 24-hour pace

| Hours | Goal |
|-------|------|
| 0–2 | Lock scope, schema, fixtures (DONE here) |
| 2–8 | Deterministic rule engine (60% of value) |
| 8–12 | Claude reasoner |
| 12–16 | Streamlit verdict UI |
| 16–20 | Wire the 5 order types; (optional) one live OIC fetch |
| 20–22 | Rehearse demo end-to-end |
| 22–24 | Buffer + slides |

## 8. Demo script (2 min)

1. "Order just got placed in CaratLane." → paste `CL-ORD-1001` → **PASS**, ₹0 at risk.
2. Paste `CL-ORD-1002` (EZ) → **FLAG**, ₹924 revenue leakage: "Barcode 1514HC1384 billed at
   ₹17,028 but BOM computes ₹17,952 — underpriced." + suggested fix.
3. Paste `CL-ORD-1003` (online) → **FLAG**: 35% discount exceeds 15% cap + coupon with ₹0 value.
4. "All deterministic — Claude writes the auditor's explanation and caught the anomaly note."
5. "In prod this fires automatically off the OIC order-placed webhook."

## 9. Run it

```bash
cd Hackathon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# CLI (works with zero API key, zero cost):
python cli.py CL-ORD-1002

# UI:
streamlit run app.py
```

Set `ANTHROPIC_API_KEY` in `.env` to turn on Claude narration; without it, Eagle Eye uses a
built-in template reasoner so the demo never breaks and cost stays ₹0.

## 10. Cost = ₹0

- Engine + UI + fallback reasoner: free, local.
- Claude narration: a few demo orders on `claude-haiku-4-5` ≈ fractions of a cent. Use the
  offline fallback if you want literally zero.
