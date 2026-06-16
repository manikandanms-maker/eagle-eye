# Eagle Eye — Requirements & Gap Analysis

> Functional + non-functional requirements, and — most importantly — the **missing
> requirements** needed to take Eagle Eye from a working hackathon demo to a production
> revenue-assurance system. Items are tagged: ✅ have · 🟡 partial · ❌ missing.

---

## 1. Functional requirements

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-1 | Audit an order from an Order ID | ✅ | CLI/API/UI |
| FR-2 | Reconcile line price vs barcode BOM | ✅ | uses `pricing_reference` (needs live pricing_engine in prod) |
| FR-3 | Validate item & order discount caps | ✅ / 🟡 | item-level done; order+item stacking (R08) pending |
| FR-4 | Validate coupon ⇄ discount value | ✅ | points/loyalty not yet modelled |
| FR-5 | Verify invoice push for shippable items | ✅ | needs ERP invoice status feed in prod |
| FR-6 | Validate delivery dates (EDD/FDD) | ✅ | FDD + courier SLA can be added |
| FR-7 | Detect ITR / sub-inventory mismatch | ✅ | ORDER vs FULFILLMENT detail |
| FR-8 | Validate customer & address master data | ✅ | live customer master check pending |
| FR-9 | Order-type restriction checks | ✅ | EZ/JM/JR/online/oldgold |
| FR-10 | Produce verdict + ₹ at risk + fix | ✅ | |
| FR-11 | Human-readable explanation | ✅ | Claude + fallback |
| FR-12 | Manufacturing / procurement checks | ❌ | hook exists; rules not written (need BOM/PO data) |
| FR-13 | Persist audit results / history | ❌ | no DB yet |
| FR-14 | Auto-trigger on order placement | 🟡 | simulated; needs webhook/Kafka subscription |
| FR-15 | Alert/route flagged orders to owners | ❌ | no notification path yet |

---

## 2. Non-functional requirements

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| NFR-1 | Audit latency < 2 s | ✅ | sub-second engine |
| NFR-2 | Deterministic money decisions | ✅ | LLM never computes money |
| NFR-3 | Zero-cost demo | ✅ | offline fallback |
| NFR-4 | Resilient to bad/missing fields | ✅ | safe parsing, per-rule isolation |
| NFR-5 | Horizontal scalability | 🟡 | engine stateless; needs worker/queue deploy |
| NFR-6 | Security / authn-authz | ❌ | API is open in demo |
| NFR-7 | Observability/metrics | 🟡 | structured findings; no metrics/log pipeline |
| NFR-8 | Auditability / reproducibility | ✅ | rule_id traceability |

---

## 3. Missing requirements to realize the production use case

### 3.1 Data & integration access ❌ (highest priority)
- **OIC / Fusion REST credentials + endpoint** to fetch the live order snapshot
  (`OIC_BASE_URL`, `OIC_AUTH_HEADER`). Today: fixtures + stub.
- **Pricing Engine read access** — the barcode BOM "computed price" must come from the real
  `pricing_engine` / `XXCL_BARCODE_COMP_PRICING_VIEW_*` views, not a fixture block.
- **ERP invoice status feed** — to verify invoice push (R10) against actual ERP sync state.
- **Sub-inventory transfer (ITR) data** — authoritative fulfilment vs ordered source.
- **`oneview_webhook` "order placed" event** subscription to auto-trigger audits.

### 3.2 Business / master data ❌
These are currently constants in `config.py`; production needs them as governed data:
- **Discount policy caps** per channel/order-type/promotion (authoritative source).
- **GST configuration** per category/state (CGST/SGST vs IGST), not a single 3%.
- **Order-restriction matrix** (which checks apply to EZ/JM/JR/online/oldgold).
- **Coupon & loyalty-points rules** (stacking, eligibility, value).
- **Financial-approval thresholds** & approver mapping.
- **Diamond/solitaire certificate requirements** by product class.

### 3.3 Platform / infrastructure ❌
- **Datastore** (Postgres) for audit history, trends, and dashboards.
- **Queue/stream** (Kafka topic `order.placed`) + **worker pool** for scale.
- **Notification/workflow** integration (oneview, Slack, email) to act on flags.
- **AuthN/AuthZ** (SSO) for the UI/API; role-based access for Revenue Assurance.
- **Secrets management** for `ANTHROPIC_API_KEY` and OIC creds (vault, not `.env`).
- **CI/CD + containerization** (Dockerfile, pipeline) for deployment.
- **Monitoring** (metrics, structured logs, alerting on engine errors).

### 3.4 AI / model ⛳
- **Anthropic API key** (optional today; recommended for richer narratives in prod).
- **Prompt/version governance** and a **golden-set evaluation** to prevent regressions.
- **PII policy** for what free-text is sent to the LLM (data-minimisation review).

### 3.5 Rules to add (functional depth) ⬜
- R08: combined item + order discount stacking cap.
- Loyalty-points redemption correctness.
- Manufacturing/procurement: BOM completeness, PO linkage, component availability.
- Tax jurisdiction correctness (state-driven CGST/SGST vs IGST).
- Courier/SLA feasibility for promised delivery dates.
- Duplicate-order / duplicate-invoice detection.

---

## 4. Dependencies summary (what to request)

| Need | From whom | Blocks |
|---|---|---|
| OIC/Fusion REST creds + order API | Integration/ERP team | Live data (FR-2, FR-5) |
| Pricing engine view access | Pricing team | R01 accuracy |
| Discount/GST/restriction policy data | Revenue Assurance / Finance | R04, R07, R16 correctness |
| Kafka `order.placed` topic access | Platform team | Auto-trigger (FR-14) |
| Postgres + deploy environment | DevOps | History/dashboard (FR-13) |
| Notification channel (oneview/Slack) | Ops | Action on flags (FR-15) |
| Anthropic API key (optional) | Project owner | Richer narration |

---

## 5. Assumptions (current demo)

- Order JSON matches `erp_data_sync/validate_order_json2.yml` structure.
- `pricing_reference.computed_price` represents the authoritative barcode price.
- GST on jewellery is a flat 3% (to be replaced by category/state config).
- One fulfilment view per item (multi-shipment splitting not yet modelled).
- Discount caps in `config.py` approximate real policy (to be confirmed).

---

## 6. Acceptance criteria (production-ready definition of done)

1. Audits a **live** order fetched from OIC in < 2 s.
2. Barcode price reconciled against the **real** pricing engine.
3. Caps/GST/restrictions sourced from **governed master data**.
4. Audit auto-triggers on **order placement**; results **persisted**.
5. Flagged orders **routed** to owners with SLA tracking.
6. API/UI behind **SSO**; secrets in **vault**.
7. Golden-set eval green; engine error rate monitored.
