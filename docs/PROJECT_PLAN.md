# Eagle Eye — Finalized Project Plan

> **Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.**

| | |
|---|---|
| **Project** | Eagle Eye — AI-powered order audit for CaratLane |
| **Event** | CaratLane Hackathon (24-hour) |
| **Version** | 1.0 (finalized) |
| **Status legend** | ✅ done · 🟡 in progress · ⬜ planned/stretch |

---

## 1. One-line pitch

Paste an **Order ID** → in seconds Eagle Eye audits the order against **every CaratLane
module** and returns **PASS / FLAG** with the **₹ at risk**, a plain-English reason, and a
fix — catching **revenue leakage the moment an order is placed**.

---

## 2. Scope

### In scope (delivered)
- Deterministic rule engine across **pricing, discount, coupon, invoice, delivery, ITR,
  customer, address, item, restriction** modules (R01–R17).
- Order types: **EZ, JM, JR, Online, Old Gold**.
- **Claude** reasoner with **offline fallback** (zero-cost guarantee).
- **SaaS web UI** (dark theme, editable Order-ID input) + **REST API** + **CLI**.
- 5 fixtures (1 PASS + 4 leakage scenarios) mirroring the real order schema.
- HLD, LLD, plan, requirements docs + tests.

### Out of scope (this hackathon)
- Automatic order remediation.
- Live OIC event subscription (simulated).
- Production master-data governance, auth, persistence (designed, not built — see §7 & REQUIREMENTS.md).

---

## 3. Deliverables & status

| # | Deliverable | Status |
|---|---|---|
| 1 | Normalized order schema + loader | ✅ |
| 2 | Deterministic rule engine + catalog | ✅ |
| 3 | Policy config (caps/GST/thresholds) | ✅ |
| 4 | Claude reasoner + offline fallback | ✅ |
| 5 | FastAPI backend + REST API | ✅ |
| 6 | SaaS dark-theme web UI (editable input) | ✅ |
| 7 | CLI client | ✅ |
| 8 | Streamlit alt UI | ✅ |
| 9 | Fixtures (PASS + 4 leakage) | ✅ |
| 10 | Sanity tests | ✅ |
| 11 | HLD / LLD / Plan / Requirements | ✅ |
| 12 | Live OIC fetch wiring | ⬜ (stub ready) |
| 13 | Findings persistence + dashboard | ⬜ stretch |
| 14 | Slides / demo recording | 🟡 |

---

## 4. 24-hour execution timeline

| Hours | Workstream | Output | Status |
|---|---|---|---|
| 0–2 | Scope, schema, fixtures | Order model + 5 fixtures | ✅ |
| 2–8 | Rule engine (60% of value) | R01–R17 + engine | ✅ |
| 8–12 | Claude reasoner | narrative + fallback | ✅ |
| 12–16 | Web UI + API | SaaS SPA + FastAPI | ✅ |
| 16–20 | Order types + (opt) live OIC | EZ/JM/JR/online/oldgold | ✅ (live = stub) |
| 20–22 | Rehearse demo | end-to-end run | 🟡 |
| 22–24 | Buffer + slides | deck | 🟡 |

---

## 5. Team roles (suggested split for a 2–4 person team)

| Role | Owns |
|---|---|
| **Rules/Engine** | `rules.py`, `engine.py`, `config.py`, fixtures, tests |
| **AI/Integration** | `reasoner.py`, `loader.py` (OIC stub), prompt tuning |
| **Frontend** | `web/*`, `server.py` API shape |
| **Demo/PM** | fixtures' narrative, slides, demo script, docs |

(If solo: build in the timeline order above — engine first, UI last.)

---

## 6. Demo script (2 minutes)

1. **Frame:** "An order was just placed in CaratLane. Eagle Eye audits it instantly."
2. `CL-ORD-1001` → ✅ **PASS**, ₹0 at risk. "Clean order — everything reconciles."
3. `CL-ORD-1002` (EZ) → 🚩 **FLAG**, **₹923.80**: barcode billed ₹17,028 but BOM computes
   ₹17,952 — underpriced. Show the suggested fix.
4. `CL-ORD-1003` (Online) → 🚩 **FLAG**: 35% discount over the 15% cap **and** a coupon that
   produced ₹0. Two modules, one order.
5. `CL-ORD-1005` (Old Gold) → 🚩 **FLAG**: ITR mismatch — a different barcode/price was
   fulfilled than ordered; missing buy-back value.
6. **Close:** "Code decides the money; Claude writes the auditor's explanation. In production
   this fires automatically off the OIC order-placed event. Zero infra cost."

---

## 7. Production roadmap (post-hackathon)

```mermaid
flowchart LR
    P1[Phase 1\nLive OIC fetch + auth] --> P2[Phase 2\nKafka order.placed subscription]
    P2 --> P3[Phase 3\nFindings DB + dashboard]
    P3 --> P4[Phase 4\nAlerting + workflow\n(oneview/Slack/email)]
    P4 --> P5[Phase 5\nPolicy master-data UI\n+ rule versioning]
```

| Phase | Goal | Key requirement |
|---|---|---|
| 1 | Real order + pricing data | OIC/Fusion REST creds; pricing_engine read access |
| 2 | Auto-trigger on placement | Subscribe to `oneview_webhook` / Kafka topic |
| 3 | History + trend | Postgres + Revenue-Assurance dashboard |
| 4 | Action, not just detection | Route flags to owners; SLA on fix |
| 5 | Business-owned rules | Policy thresholds + rules as managed data |

---

## 8. Success criteria

| Metric | Target |
|---|---|
| Audit latency | < 2 s per order |
| Money-decision correctness | 100% deterministic (no LLM math) |
| Demo reliability | Runs at ₹0 with no API key |
| Coverage | All listed modules + 5 order types |
| Extensibility | New rule added in < 15 min |

---

## 9. Cost

| Item | Cost |
|---|---|
| Engine + UI + CLI + fallback reasoner | ₹0 (local, stdlib/open-source) |
| Claude narration (optional) | ~fractions of a cent per order (Haiku) |

See **REQUIREMENTS.md** for the dependencies needed to move beyond the demo.
