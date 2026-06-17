# CEagle Eye — Presentation Guide & Panel Q&A

> **Use this doc to present CEagle Eye to leadership, ops, engineering, or audit panels.**  
> It explains what we built, why it matters, how it works end-to-end, and anticipated questions.

---

## 1. Elevator pitch (30 seconds)

**CEagle Eye** is CaratLane’s **order first-check cockpit**. Operations or audit teams paste an order number and, within seconds, see whether that order is safe to fulfill — across **Magento**, **Fusion PaaS**, **Fusion SaaS**, Vendor QC, and internal SOP rules.

Unlike a periodic SOC audit, CEagle Eye runs **per order, in real time**, and routes failures to the right team (Service, Buy, Finance, ERP, Manufacturing).

**Tagline:** *Watching Every Transaction · Predicting Every Risk · Protecting Every Fulfillment*

---

## 2. The business problem

### Order journey (where things break)

```
Store / Online → Magento → OIC (SaaS) → PaaS sync → Oracle Fusion ERP → back to PaaS
```

Money and fulfillment risk hide in the gaps:

| Risk | Example |
|------|---------|
| Pricing leakage | Barcode billed below BOM-computed price |
| Discount abuse | Coupon + manual discount stacking beyond policy |
| ERP sync gap | Order not pushed, barcode not reserved, ATP missing |
| Cross-system drift | CL location ≠ PaaS location ≠ SaaS location |
| Manufacturing | JW PO missing, Vendor QC reject, GRN incomplete |
| Finance | AR open, high-value order without review |
| Stale orders | Processing > 90 days |

Traditional audits are **periodic** and **sample-based**. CEagle Eye is **continuous** and **order-centric**.

---

## 3. What we built (demo flow)

### Step 1 — Validate (live production data)

1. User enters order: `hash_id`, `increment_id`, or `entity_id`
2. Backend loads order from **Magento read-replica**
3. Enriches each barcode line from **Fusion DB + Fusion BI SOAP + Vendor QC App**
4. Runs **~13 pre-audit checks** + **~10 active SOP checks** (some finance/WO checks intentionally disabled for v1)
5. UI shows:
   - Summary bar (Passed / Failed / Warning + progress)
   - Filterable checklist (Failed first, sections collapsed if all pass)
   - Customer card + integration chips
   - Order detail table (all enriched fields)
   - Work orders table (Fusion)

6. Optional: **Google Chat** posts pass/fail summary (incl. high-value > ₹5L alert)

### Step 2 — Run Audit (revenue leakage engine)

Only enabled when all first-checks pass. Runs **R01–R17** deterministic rules + optional **Claude** narration.

**Hybrid Safety:** Python decides pass/fail and ₹ at risk; AI only explains — never approves money.

---

## 4. Architecture (for technical panel)

```
┌─────────────┐     ┌──────────────────────────────────────────┐
│  Web UI     │────▶│  FastAPI (server.py)                      │
│  app.js     │     │  GET /api/validate/{order}               │
└─────────────┘     │  GET /api/audit/{order}                  │
                    └──────────────┬───────────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         ▼                         ▼                         ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│ Magento MySQL   │    │ Fusion PaaS DB   │    │ Fusion SaaS SOAP    │
│ (read replica)  │    │ (Oracle ATP)     │    │ (BI Publisher)      │
│                 │    │                  │    │                     │
│ orders, lines,  │    │ mfg type, GRN,   │    │ ATP/WD/UOM, barcode │
│ invoices, QC    │    │ barcode trx, WO  │    │ loc/txn, AR/AP      │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
```

**Key design choices:**

- **Read-only** everywhere — no writes to Magento or Fusion
- **Connection pooling** on Magento — faster repeat validations
- **Per-item SOAP** for ATP on cache miss — accurate but ~2–10s per unique SKU
- **Slim API payload** — large JSON (`price_breakup`) stripped from UI response
- **Checks are modular** — each returns `{passed, actual, expected, action}`

---

## 5. Integrations deep-dive (what to say when asked “what’s connected?”)

| Integration | Source | What we validate |
|-------------|--------|------------------|
| Order header | Magento `sales_flat_order` | Fusion flag, pushed to ERP, customer email |
| Barcode lines | `sales_flat_order_item_qty` | Barcode present, EDD min/max, ERP status |
| **ERP status** | Line `erp_status` | On Hold, Processing, Dispatched, **Cancelled** (all in scope) |
| Invoice | `caratlane_invoices.meta.itemIds` | Invoice number per item_id |
| CL barcode | `erp_barcode_attributes` | Location, transaction type, statuses |
| PaaS barcode | `XXCL_BARCODE_TRX_LOC_DETAILS` | Organization, transaction, statuses |
| SaaS barcode | BI reports (`lot_number` param) | Location + transaction (60+ txn code map) |
| ATP/WD/UOM | SaaS `item_wd_atp_uom.xdo` | PRESENT / MISSING per SKU |
| Manufacturing | Fusion item structure | Inhouse / JW / Outright |
| Inhouse bag | Fusion bag tables | Bag status, metal loss (display only; SOP check off) |
| JW GRN | `xxcl_vndr_add_grn` | GRN status, PO number |
| Vendor QC | `indus_purchase_orders_item_qty` | Latest QC status by barcode + PO |
| Work orders | `XXCL_WIE_WORK_ORDERS_B` | PO, ASBN, GRN, planned dates |
| AR invoice | SaaS AR report | Invoice status (Dispatched orders) |
| Google Chat | Webhook | Every search → team channel |

---

## 6. ERP status & Cancelled lines

**Cancelled is included** in first-check scope:

- Cancelled barcode lines **appear in the order detail table**
- ERP status check accepts: `On Hold`, `Processing`, `Dispatched`, `Cancelled` / `Canceled`
- Useful for **audit trail** and **partial cancellation** scenarios (mixed Processing + Cancelled on same order)

**Presentation talking point:**  
“We don’t hide cancelled lines — ops can see the full order picture and understand why validation failed or passed on active vs cancelled qty.”

---

## 7. Checks currently active vs disabled (v1 honesty)

| Check | Status | Why |
|-------|--------|-----|
| Fusion order, pushed, barcode, EDD, customer | ✅ Active | Core gates |
| CL / PaaS / SaaS location & transaction | ✅ Active | Cross-system sync |
| Price breakup | ✅ Active | Revenue protection |
| SOP: PAN, stale order, barcode reserved, MTO, PO, ATP | ✅ Active | Ops playbook |
| SOP: QC/GRN, RM consumption, AR status | ✅ Active | Buy + Finance signals |
| SOP: High-value > ₹5L | ✅ Warning | Finance review flag |
| AP payment / AP ledger ID | ⏸ Disabled | Pending Finance sign-off |
| WO completion (planned date) | ⏸ Disabled | GRN can be Complete while planned date is stale — need Fusion WO status field |
| Inhouse metal loss | ⏸ Disabled | Data sparsity on some Inhouse lines |
| ILO profile balance | ⏸ Placeholder | Integration not available |

**Panel answer:** “We shipped the highest-signal checks first. Disabled items are commented in code and can be toggled on when business rules are finalized.”

---

## 8. User impact — who benefits and how

| Persona | Before CEagle Eye | After CEagle Eye |
|---------|-------------------|----------------|
| **Store ops / Service** | Manual SQL + 5 tools to answer “why is this order stuck?” | One search → checklist + escalation action |
| **Buy / JW team** | PO + QC + GRN checked separately | Single view: PO, Vendor QC, GRN, work orders |
| **ERP team** | ATP/WD/UOM checked in Fusion UI per SKU | Auto-fetched per order line |
| **Finance** | High-value orders found late | ₹5L+ flagged on validate + Chat alert |
| **Audit / Risk** | Sample-based monthly review | Every order can be first-checked before ship |
| **Leadership** | No single pane of glass | Pass/fail %, integration health, order value |

**Quantified potential (frame carefully):**

- **Time:** Manual first-check ~15–30 min/order → **< 1 min** automated (excluding cold ATP SOAP)
- **Leakage:** R01–R17 audit catches mispricing, discount abuse, invoice gaps — **₹ per order** surfaced in audit step
- **Escalation:** Failed check includes **named team** in `action` field — reduces ticket ping-pong

---

## 9. Demo script (5 minutes)

1. **Open** `http://127.0.0.1:8000` — show CEagle Eye branding, single “Order Audit” nav
2. **Healthy order** — e.g. `EZDELSHD2AF2I-JM`  
   - Green summary, collapsed passed sections, ATP PRESENT, integrations chips
3. **Mixed / complex order** — e.g. `EZTRCKBR789CP-JM`  
   - JW + Inhouse lines, Vendor QC COMPLETED, SaaS location aligned, work orders table
4. **High-value** — order with `net_payable` > ₹5L  
   - Warning pill in summary, Google Chat mention
5. **Show filters** — “Issues only” → only failed/warn checks
6. **Order detail table** — scroll CL / PaaS / SaaS columns, erp_status including Cancelled if applicable
7. **Optional:** Run Audit on passing order → FLAG example with ₹ at risk

---

## 10. Anticipated panel questions & suggested answers

### Product & scope

**Q: Is this production-ready?**  
A: Validate path runs on **live read replicas** and Fusion read-only creds. It’s suitable for **ops pilot** and **audit assist**. Full audit (R01–R17) on live OIC feed is the next integration step.

**Q: Why not fix issues in-system instead of only reporting?**  
A: v1 is **read-only by design** — zero risk to production data. Future: webhook to create Jira/ServiceNow tickets from failed `action` fields.

**Q: Does it block order fulfillment automatically?**  
A: Not today. It **informs** and **notifies** (Google Chat). Hooking into OIC “hold shipment” is a roadmap item.

**Q: Why include Cancelled lines?**  
A: Orders often have **partial cancellation**. Auditors need the full line-level picture, not a filtered subset that hides cancelled qty.

---

### Technical

**Q: How fast is it?**  
A: Magento + Fusion DB enrichment: **~1–3s** typical. Cold ATP SOAP adds **~2–10s per unique SKU** (parallelized, cached in memory). Bulk ATP cache refresh available offline.

**Q: What if Fusion SOAP is down?**  
A: Checks degrade gracefully — ATP shows `MISSING`, SaaS barcode columns empty, integrations chip shows state (`cache_miss`, `run_report_failed`). Magento checks still run.

**Q: How do you compare CL vs PaaS vs SaaS transaction types?**  
A: We normalize via **60+ transaction code mappings** (e.g. `POR` ↔ `Purchase Order Receipt`) before comparing.

**Q: Is the LLM making pass/fail decisions?**  
A: **No.** Hybrid Safety — Python rules are source of truth. Claude only writes the audit narrative in Step 2.

**Q: Security / credentials?**  
A: All DB/SOAP creds in `.env`, gitignored. Read-only Oracle wallet + Magento RR user. No PII logged to Chat beyond order hash and customer email in summary.

---

### Data & accuracy

**Q: How accurate is Vendor QC?**  
A: Latest status from `indus_purchase_orders_item_qty` JSON timestamps, matched by **(barcode, PO)** with barcode-only fallback. If barcode isn’t in Vendor QC App, shows `MISSING` — not a false pass.

**Q: Why did WO show OVERDUE when GRN was Complete?**  
A: v1 used **planned_completion_date vs today** only. We **disabled** that check until Fusion exposes actual WO completion status.

**Q: Invoice mapping — how?**  
A: `caratlane_invoices.meta.itemIds` JSON array joined to `sfoiq.item_id` — not legacy barcodeInfo parsing.

---

### Business & ROI

**Q: What’s the ROI?**  
A: (1) **Time saved** on manual first-check, (2) **Revenue leakage prevented** via pricing/discount rules, (3) **Faster escalation** to correct team, (4) **Audit evidence** per order with timestamped Chat post.

**Q: Which team owns this?**  
A: Suggested: **Revenue Assurance / Internal Audit** with **ERP** and **Service** as integration owners. CEagle Eye is the **orchestration layer**, not a replacement for Fusion/Magento.

**Q: How is this different from existing reports?**  
A: Reports are **system-specific**. CEagle Eye is **order-centric** and **cross-system** — one order, one verdict, one escalation path.

---

## 11. Further enhancements (roadmap talking points)

### Near term (1–2 sprints)

| Enhancement | Benefit |
|-------------|---------|
| Re-enable AP / ledger checks with Finance rules | Vendor settlement compliance |
| WO completion from Fusion status field (not planned date) | Accurate Buy-path signal |
| Jira / ServiceNow auto-ticket from `action` | Close the loop on failures |
| Skip SOAP for Cancelled lines only | Faster validation on mixed orders |
| Batch validate (CSV upload) | Store audit batches overnight |

### Medium term (1–2 quarters)

| Enhancement | Benefit |
|-------------|---------|
| OIC webhook on order placed → auto-validate | Zero manual paste |
| Live OIC feed for R01–R17 audit | End-to-end on production orders |
| ILO profile balance integration | Complete SOP #14 |
| Role-based UI (ops vs finance vs audit) | Less noise per persona |
| Dashboard: pass rate by store, SKU, vendor | Management metrics |

### Long term

| Enhancement | Benefit |
|-------------|---------|
| ML anomaly layer on top of rules | Catch unknown patterns |
| Pre-shipment API gate in fulfillment | Block ship on CRITICAL fail |
| Historical trend: “this store fails barcode sync 12%” | Proactive coaching |

---

## 12. Risks & mitigations (panel will ask)

| Risk | Mitigation |
|------|------------|
| SOAP latency on large orders | ATP cache, parallel SKU fetch, optional sync-on-miss off |
| False positives on barcode sync | Transaction code normalization + manual review UI |
| Credential exposure | `.env` only, read-only users, no secrets in repo |
| Over-reliance on AI | Hybrid Safety — rules engine is authoritative |
| Scope creep | Modular checks — enable/disable per SOP agreement |

---

## 13. Metrics to track post-pilot

- **% orders passing first-check** (by store, channel, mfg type)
- **Top 5 failing checks** (Pareto for fix investment)
- **Mean time to diagnose** (before/after CEagle Eye)
- **₹ flagged** in full audit step
- **Chat alert → resolution time** for high-value orders

---

## 14. One-slide summary (copy-paste)

**CEagle Eye** = CaratLane order first-check + revenue audit

- **Input:** Order number  
- **Output:** Pass/fail checklist, enriched cross-system data, team escalation  
- **Systems:** Magento + Fusion PaaS + Fusion SaaS + Vendor QC + Chat  
- **Principle:** Code decides money; AI explains only  
- **Status:** Live validate pilot; selective SOP checks; audit on fixtures + engine ready  
- **Impact:** Faster ops, fewer cross-system surprises, audit trail per order  

---

## 15. Related documents

| Doc | Purpose |
|-----|---------|
| [README.md](../README.md) | Technical integration reference |
| [HLD.md](HLD.md) | Architecture |
| [LLD.md](LLD.md) | API + data model |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Gaps for production |

---

*Prepared for CEagle Eye presentation — CaratLane Revenue Assurance / Hackathon.*
