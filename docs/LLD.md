# Eagle Eye — Low-Level Design (LLD)

> Module-by-module design, data structures, API contracts, the full rule catalog, and
> extension points. Read **HLD.md** first for the big picture.

| | |
|---|---|
| **Document** | Low-Level Design |
| **Version** | 1.0 |
| **Code root** | `Hackathon/` |

---

## 1. Module map

```
Hackathon/
├── server.py            # FastAPI app: REST API + serves SPA
├── cli.py               # zero-dependency terminal client
├── app.py               # alternative Streamlit UI
├── audit/
│   ├── config.py        # policy thresholds (GST, caps, tolerances, severity)
│   ├── schema.py        # Order/Item models + safe parsing + order-type normalization
│   ├── loader.py        # COLLECT: fixture/OIC fetch + normalize
│   ├── rules.py         # DECIDE: Finding dataclass + rule catalog (R01..R17)
│   ├── engine.py        # DECIDE: orchestrates rules -> AuditResult
│   └── reasoner.py      # EXPLAIN: Claude + offline template fallback
├── web/                 # SPA: index.html, style.css, app.js
├── data/orders/*.json   # fixtures (mirror real CaratLane order shape)
└── tests/test_engine.py # rule-engine sanity tests
```

Data-flow dependency (acyclic): `config → schema → loader → rules → engine → reasoner → server/cli/app`.

---

## 2. Data model

### 2.1 Raw order (input) — mirrors `erp_data_sync/validate_order_json2.yml`
```jsonc
{
  "order_header": { "order_no", "order_type", "order_date", "customer_id",
                    "coupon_code", "discount_amount", "sub_total", "tax",
                    "grand_total", "financial_approval", "ship_pin_code",
                    "ship_state", "payment_mode", "old_gold_value", ... },
  "order_items": [
    {
      "pricing_reference": { "barcode", "metal_value", "making_charge",
                             "stone_value", "computed_price" },   // barcode BOM truth
      "ORDER DETAIL":      { "sku","barcode","price","discount_percent",
                             "flat_discount","price_before_tax","tax","amount",
                             "edd_date","status","is_diamond", ... },
      "FULFILLMENT DETAIL":{ "sku","barcode","price","price_before_tax","tax",
                             "amount","invoice_no","certificate_no","status", ... }
    }
  ],
  "order_billing_address": [ ... ],
  "order_shipping_address": [ ... ]
}
```

### 2.2 Normalized model (`schema.py`)
```python
class Item:
    detail: dict            # ORDER DETAIL
    fulfillment: dict       # FULFILLMENT DETAIL
    pricing_reference: dict # barcode BOM
    # accessors: sku, barcode, product_name, is_diamond
    # f(key)  -> float from ORDER DETAIL
    # ff(key) -> float from FULFILLMENT DETAIL

class Order:
    order_id: str
    order_type: str         # EZ | JM | JR | ONLINE | OLDGOLD  (normalized)
    header: dict
    items: list[Item]
    billing: list[dict]
    shipping: list[dict]
    raw: dict
    # h(key) -> float from header
```

**Helpers**
- `to_float(v)` — `'17028.2'→17028.2`, `''/'NA'/None→None` (never raises).
- `is_blank(v)` — None/empty/`NA`/`null`/`none`.
- `normalize_order_type(type, source)` — maps many real strings to the 5 buckets.

### 2.3 Finding (rule output, `rules.py`)
```python
@dataclass
class Finding:
    module: str       # PRICING | DISCOUNT | COUPON | INVOICE | DELIVERY | ITR | ITEM | CUSTOMER | ADDRESS | RESTRICTION
    rule_id: str      # R01..R17
    title: str
    status: str       # PASS | FAIL
    severity: str     # CRITICAL | HIGH | MEDIUM | LOW | INFO
    expected: str
    actual: str
    rupee_impact: float
    detail: str
    line: str | None  # "sku / barcode" if line-level
```

### 2.4 AuditResult (`engine.py`)
```python
class AuditResult:
    order: Order
    findings: list[Finding]
    failures        -> [f for f in findings if not f.ok]
    verdict         -> "FLAG" if any failure severity >= FLAG_AT else "PASS"
    rupees_at_risk  -> sum(f.rupee_impact for f in failures)
    top_severity    -> worst failing severity
    sorted_failures()-> by (severity desc, rupee_impact desc)
    summary()       -> dict for the API
```

---

## 3. Policy configuration (`config.py`)

| Constant | Value (demo) | Meaning |
|---|---|---|
| `GST_RATE` | `0.03` | Gold jewellery GST (3%) |
| `MONEY_TOLERANCE` | `1.0` | ₹ rounding tolerance for equality checks |
| `DISCOUNT_CAP` | EZ 20, JM 15, JR 15, ONLINE 15, OLDGOLD 25, DEFAULT 15 | Max effective discount % per channel |
| `FINANCIAL_APPROVAL_THRESHOLD` | `100000` | ₹ above which `financial_approval=YES` required |
| `SHIPPABLE_STATUSES` | Dispatched/Shipped/Complete/Invoiced/Delivered | Must carry an invoice |
| `SEVERITY_ORDER` | CRITICAL>HIGH>MEDIUM>LOW>INFO | Ranking |
| `FLAG_AT` | `HIGH` | Verdict threshold |

> In production these come from ERP/policy master data, not constants (see REQUIREMENTS.md §4).

---

## 4. Rule catalog (full LLD)

Each rule is `f(order: Order) -> list[Finding]`. Returning `[]` = pass. A try/except in the
engine isolates failures. **No rule performs I/O or calls the LLM.**

| ID | Module | Predicate (PASS condition) | Severity on fail | ₹ impact formula |
|----|--------|----------------------------|------------------|------------------|
| **R01** | PRICING | `item.price == pricing_reference.computed_price` (±tol) | CRITICAL if |Δ|≥500 else HIGH | `max(computed − price, 0)` |
| **R02** | PRICING | `price_before_tax == price − discount` | HIGH | `|pbt − expected|` |
| **R03** | PRICING | `amount == price_before_tax + tax` | HIGH | `|amount − (pbt+tax)|` |
| **R04** | PRICING | `tax ≈ price_before_tax × GST_RATE` (±max(tol,1%)) | HIGH if under else MEDIUM | `max(expected_tax − tax, 0)` |
| **R05** | PRICING | `header.sub_total == Σ price_before_tax` | HIGH | `|Δ|` |
| **R06** | PRICING | `grand_total == sub_total + tax` | HIGH | `|Δ|` |
| **R07** | DISCOUNT | `effective_discount% ≤ cap[order_type]` | HIGH | `(eff−cap)/100 × price` |
| **R09** | COUPON | `coupon_code present ⇔ discount_amount>0` | HIGH (value w/o code) / MEDIUM (code w/o value) | discount value when leaking |
| **R10** | INVOICE | shippable item ⇒ `invoice_no` present | CRITICAL | item `amount` (unbillable) |
| **R11** | DELIVERY | `edd_date` present and `≥ order_date` | HIGH (before) / MEDIUM (missing) | 0 |
| **R12** | ITR | `ORDER DETAIL` ⇔ `FULFILLMENT DETAIL` barcode & price & amount | CRITICAL (barcode) / HIGH (money) | `|Δ|` |
| **R13** | ITEM | `is_diamond ⇒ certificate_no present` | HIGH | 0 |
| **R14** | CUSTOMER | customer id+name present; email well-formed | MEDIUM / LOW | 0 |
| **R15** | ADDRESS | 6-digit pincode + state present | MEDIUM | 0 |
| **R16** | RESTRICTION | high-value⇒approval; ONLINE⇒payment; OLDGOLD⇒buy-back value | HIGH | 0 |
| **R17** | PRICING | `price>0` and barcode+sku present | CRITICAL / HIGH | 0 |

> R08 (item+order discount stacking) is reserved/planned — see REQUIREMENTS.md.

**Registration:** add a function and append it to `ALL_RULES` in `rules.py`. Nothing else changes.

---

## 5. Engine algorithm (`engine.py`)

```
def run_rules(order):
    findings = []
    for rule in ALL_RULES:
        try:        produced = rule(order) or []
        except e:   produced = [Finding(ENGINE, "Rule error", LOW, ...)]
        findings.extend(produced)
    return AuditResult(order, findings)

verdict        = FLAG if max(severity of failures) >= FLAG_AT else PASS
rupees_at_risk = Σ rupee_impact over failures
```
Complexity: `O(R × I)` (rules × items) — effectively constant per order, sub-second.

---

## 6. Reasoner (`reasoner.py`)

```
explain(result):
    if no ANTHROPIC_API_KEY or no SDK: return _offline(result)
    try:
        msg = Claude.messages.create(system=SYSTEM, payload=_payload(result))
        return parse_json(msg)   # {verdict, issues[], anomalies[], engine}
    except: return _offline(result)   # graceful, demo never breaks
```

- `_payload` sends **only**: order id/type, verdict, ₹ risk, free-text fields, and the
  already-computed findings. The model is instructed **not to recompute numbers or change
  verdicts**.
- `_offline` builds the same JSON shape from findings + a per-rule fix table (`_suggest_fix`).
- Output contract (consumed by UI/CLI):
```jsonc
{ "verdict": str,
  "issues": [{ "title","severity","rupee_impact","explanation","fix","line" }],
  "anomalies": [str],
  "engine": "claude-haiku-4-5" | "offline-fallback" }
```

---

## 7. API contract (`server.py`)

Base: `http://127.0.0.1:8000`

| Method | Path | Response |
|---|---|---|
| `GET` | `/api/orders` | `{ "orders": ["CL-ORD-1001", ...] }` |
| `GET` | `/api/audit/{order_id}` | audit payload (below); `404` if unknown |
| `GET` | `/` | SPA `index.html` |
| `GET` | `/static/*` | CSS/JS assets |
| `GET` | `/docs` | auto OpenAPI (FastAPI) |

**`GET /api/audit/{id}` →**
```jsonc
{
  "summary": { "order_id","order_type","verdict","top_severity",
               "rupees_at_risk","failure_count","checks_run" },
  "narrative": { "verdict","issues":[...],"anomalies":[...],"engine" },
  "findings": [ { "module","rule_id","severity","title","expected",
                  "actual","rupee_impact","line","detail" } ]
}
```

---

## 8. Frontend (`web/`)

- **`index.html`** — SaaS shell: sidebar (brand, nav, reasoner pill), top bar (title +
  tagline + env badge), search card with **editable Order-ID input** (`<input list=datalist>`),
  quick-pick chips, KPI grid, narrative card, findings list, anomalies, raw table.
- **`style.css`** — dark theme tokens (CSS variables), indigo primary `#6d5efc`, gold accent,
  responsive (sidebar collapses < 860px).
- **`app.js`** — `loadOrders()` populates datalist+chips; `runAudit()` calls API, Enter-key
  submits, spinner state, `render()` paints KPIs/issues/anomalies/raw table, `showError()` for
  unknown IDs. All values HTML-escaped (`esc`).

---

## 9. Error handling matrix

| Failure | Behaviour |
|---|---|
| Unknown order id | `loader` raises `KeyError` → API `404` → UI inline error with known IDs |
| Rule throws | caught per-rule → LOW "Rule error" finding; audit continues |
| LLM key missing/invalid/network | template fallback; `engine` field notes the reason |
| Malformed money field | `to_float` → `None`; rule skips that comparison (no false fail) |
| Missing fulfillment block | ITR/invoice rules skip rather than false-fail |

---

## 10. Testing (`tests/test_engine.py`)

Asserts verdict + that the **intended rule IDs fire** per fixture:

| Fixture | Verdict | Rules asserted |
|---|---|---|
| CL-ORD-1001 | PASS | — |
| CL-ORD-1002 | FLAG | R01 |
| CL-ORD-1003 | FLAG | R07, R09 |
| CL-ORD-1004 | FLAG | R10, R11 |
| CL-ORD-1005 | FLAG | R12, R16 |

Run: `python tests/test_engine.py` (or `pytest tests/`).

---

## 11. Extension recipes

- **New rule:** write `rNN_x(order)->[Finding]`, append to `ALL_RULES`, add a fix line in
  `_suggest_fix`, add a fixture + test.
- **New order type:** extend `normalize_order_type` + add a `DISCOUNT_CAP` entry.
- **Live data:** implement `fetch_from_oic` (uncomment `requests` block), set `OIC_BASE_URL`/`OIC_AUTH_HEADER`, call `audit(id, source="oic")`.
- **Persistence:** write `AuditResult.summary()` + findings to Postgres in `server.audit_order`.
