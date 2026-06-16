const $ = (id) => document.getElementById(id);
const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let KNOWN = [];
let VALIDATED_ORDER = null;
let DATA_SOURCE = "fixtures";

async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    DATA_SOURCE = h.data_source || "fixtures";
    const badge = $("envBadge");
    const magento = h.magento_db === "configured";
    const fusion = h.fusion_db === "configured";
    const soap = h.fusion_soap === "configured";
    const cache = h.fusion_report_cache || {};
    if (magento && soap && cache.cache_fresh) badge.textContent = "🟢 Magento + Fusion + ATP cache";
    else if (magento && fusion) badge.textContent = "🟢 Magento + Fusion DB";
    else if (magento) badge.textContent = "🟢 Magento · Fusion pending";
    else badge.textContent = "🟡 Fixtures · set MAGENTO_DB_PASSWORD";
  } catch (_) { /* server may still be booting */ }
}

async function loadOrders() {
  try {
    const { orders } = await (await fetch("/api/orders")).json();
    KNOWN = orders || [];
    $("orderList").innerHTML = KNOWN.map((o) => `<option value="${esc(o)}">`).join("");
    $("chips").innerHTML =
      `<span class="muted" style="align-self:center">Demo:</span>` +
      KNOWN.map((o) => `<button class="chip" data-id="${esc(o)}">${esc(o)}</button>`).join("");
    document.querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => { $("order").value = c.dataset.id; runValidate(); })
    );
  } catch (_) { /* server may still be booting */ }
}

async function runValidate() {
  const id = $("order").value.trim();
  if (!id) { $("order").focus(); return; }

  VALIDATED_ORDER = null;
  $("auditBtn").disabled = true;
  $("result").classList.add("hidden");

  const btn = $("validateBtn");
  btn.disabled = true;
  btn.textContent = "Validating…";

  try {
    const res = await fetch(`/api/validate/${encodeURIComponent(id)}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `Validation failed (${res.status})`);
    }
    renderValidation(body);
    VALIDATED_ORDER = body.valid ? (body.order_number || id) : null;
    $("auditBtn").disabled = !body.valid;
  } catch (e) {
    showError(e.message);
    $("validation").classList.add("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Validate";
  }
}

function renderValidation(data) {
  $("empty").classList.add("hidden");
  $("validation").classList.remove("hidden");

  const passed = data.valid;
  const searched = data.order_number || "—";
  const meta = data.meta || [
    data.hash_id && `hash: ${data.hash_id}`,
    data.entity_id != null && `entity_id: ${data.entity_id}`,
  ].filter(Boolean).join(" · ");

  const summary = $("validationSummary");
  summary.className = "validation-summary " + (passed ? "pass" : "fail");
  summary.innerHTML = passed
    ? `✅ <b>${esc(data.message)}</b><br/><span class="muted">Searched: <b>${esc(searched)}</b>${meta ? " · " + esc(meta) : ""} · ${data.line_count} barcode(s)</span>`
    : `⚠️ <b>${esc(data.message)}</b><br/><span class="muted">Searched: <b>${esc(searched)}</b>${meta ? " · " + esc(meta) : ""} · fix failing checks below</span>`;

  $("validationMeta").textContent = passed ? "all checks passed" : "checklist failed";
  $("lineCount").textContent = data.line_count || 0;

  renderCustomerCard(data.customer || {}, data.integrations || {});

  const checks = data.checks || [];
  $("checklistOrder").innerHTML = renderChecklist(checks.filter((c) => c.module === "ORDER"));
  $("checklistCustomer").innerHTML = renderChecklist(checks.filter((c) => c.module === "CUSTOMER"));
  $("checklistBarcode").innerHTML = renderChecklist(checks.filter((c) => c.module === "BARCODE"));

  $("orderDetails").innerHTML = orderDetailsTable(data.lines || [], data.header || {}, data.customer || {});
  renderWorkOrders(data.work_orders || []);
}

function renderWorkOrders(rows) {
  const wrap = $("workOrdersWrap");
  const el = $("workOrders");
  if (!rows.length) {
    wrap.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  wrap.classList.remove("hidden");
  const cols = [
    "work_order_number", "manufacturing_type", "po_number", "asbn_number",
    "grn_number", "grn_status", "planned_start_date", "planned_completion_date",
  ];
  const body = rows.map((r) =>
    `<tr>${cols.map((c) => `<td>${cellVal(r, c)}</td>`).join("")}</tr>`
  ).join("");
  el.innerHTML = `<div class="table-scroll"><table><thead><tr>${cols.map((c) =>
    `<th>${esc(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderCustomerCard(c, integrations) {
  const card = $("customerCard");
  if (!c || !Object.keys(c).length) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  const fields = [
    ["Searched as", c.searched_as, true],
    ["Customer name", c.name],
    ["Email", c.email],
    ["PAN", c.pan_no],
    ["Fusion party #", c.fusion_party_number],
    ["Hash ID", c.hash_id],
    ["Source", c.source],
  ];
  let html = fields.map(([label, val, hi]) =>
    `<div class="customer-field">
      <span class="customer-label">${esc(label)}</span>
      <span class="customer-value${hi ? " highlight" : ""}">${esc(val || "—")}</span>
    </div>`
  ).join("");
  if (integrations && Object.keys(integrations).length) {
    const bits = [];
    if (integrations.invoice) bits.push("invoice");
    if (integrations.manufacturing) bits.push("mfg type");
    if (integrations.inhouse_bag) bits.push("inhouse bag");
    if (integrations.jw_grn) bits.push("JW GRN");
    if (integrations.vendor_qc) bits.push("vendor QC");
    if (integrations.item_atp_wd_uom && integrations.item_atp_wd_uom !== "cache_miss") {
      bits.push(`ATP/WD/UOM (${integrations.item_atp_wd_uom})`);
    } else if (integrations.fusion_soap) {
      bits.push("ATP/WD/UOM cache empty — run refresh");
    }
    if (integrations.paas_barcode_trx) bits.push("PaaS trx/loc");
    if (integrations.saas_barcode_location === "disabled") bits.push("SaaS loc (off)");
    else if (integrations.saas_barcode_location && integrations.saas_barcode_location !== "run_report_failed") {
      bits.push(`SaaS loc (${integrations.saas_barcode_location})`);
    }
    if (integrations.saas_barcode_transaction === "disabled") bits.push("SaaS txn (off)");
    else if (integrations.saas_barcode_transaction && integrations.saas_barcode_transaction !== "run_report_failed") {
      bits.push(`SaaS txn (${integrations.saas_barcode_transaction})`);
    }
    if (integrations.work_orders) bits.push("work orders");
    if (integrations.fusion_db && !integrations.manufacturing) bits.push("fusion: no mfg rows");
    if (bits.length) {
      html += `<div class="customer-field" style="grid-column:1/-1">
        <span class="customer-label">Integrations</span>
        <span class="customer-value muted">${esc(bits.join(" · "))}</span>
      </div>`;
    }
  }
  card.innerHTML = html;
}

function renderChecklist(checks) {
  if (!checks.length) {
    return `<p class="muted" style="padding:8px 2px">No checks in this module.</p>`;
  }
  return checks.map((c) => {
    const ok = c.passed;
    return `<div class="check-item ${ok ? "ok" : "bad"}">
      <div class="check-head">
        <span class="check-icon">${ok ? "✓" : "✗"}</span>
        <span>${esc(c.label)}</span>
      </div>
      <div class="check-actual">${esc(c.actual)} <span class="muted">· expected: ${esc(c.expected)}</span></div>
      <div class="check-detail">${esc(c.detail)}</div>
    </div>`;
  }).join("");
}

function cellVal(ln, key) {
  const v = ln[key];
  if (v == null || v === "") return "—";
  if (typeof v === "boolean") return v ? "YES" : "NO";
  if (key === "final_price" || key === "price_before_tax" || key === "tax" || key === "loss_stock_value") {
    const n = Number(v);
    return Number.isFinite(n) ? inr(n) : esc(v);
  }
  return esc(v);
}

function orderDetailsTable(lines, header, customer) {
  if (!lines.length) return "<p class='muted' style='margin-top:10px'>No barcode lines returned.</p>";

  const headerBits = [
    customer.searched_as && `searched: ${esc(customer.searched_as)}`,
    header.hash_id && `hash: ${esc(header.hash_id)}`,
    header.net_payable != null && `net: ${inr(header.net_payable)}`,
  ].filter(Boolean).join(" · ");

  const baseCols = [
    "barcode", "item_number", "name", "erp_status",
    "expected_delivery_date_min", "expected_delivery_date_max",
    "atp_status", "wd_status", "uom_status",
    "invoice_number", "invoice_barcode", "invoice_flag",
    "make_buy", "vendor_name", "manufacturing_type",
    "cl_location_name", "paas_organization_name", "saas_location_name", "location_mismatch",
    "cl_transaction_type", "paas_transaction_type", "saas_transaction_type", "transaction_mismatch",
    "duplicate_onhand_count", "sold_onhand_present",
    "price_before_tax", "tax", "final_price", "location_name",
  ];
  const extraCols = [];
  const hasInhouse = lines.some((ln) => ln.manufacturing_type === "Inhouse");
  const hasJw = lines.some((ln) => ln.manufacturing_type === "JW");
  if (hasInhouse) {
    extraCols.push("bag_status", "factory", "loss_weight", "loss_stock_value");
  }
  if (hasJw) {
    extraCols.push(
      "grn_transaction_number", "grn_status", "grn_gross_weight",
      "vendor_qc_status", "vendor_qc_status_time"
    );
  }
  const cols = [...baseCols, ...extraCols];

  const rows = lines.map((ln) =>
    `<tr>${cols.map((c) => `<td>${cellVal(ln, c)}</td>`).join("")}</tr>`
  ).join("");

  return `<p class="muted" style="margin-top:10px;margin-bottom:8px">${headerBits}</p>
    <div class="table-scroll">
    <table><thead><tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead>
    <tbody>${rows}</tbody></table>
    </div>`;
}

async function runAudit() {
  const id = VALIDATED_ORDER || $("order").value.trim();
  if (!id) { $("order").focus(); return; }

  const btn = $("auditBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> Auditing…`;
  try {
    const res = await fetch(`/api/audit/${encodeURIComponent(id)}?source=auto`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Audit failed (${res.status})`);
    }
    render(await res.json());
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = !VALIDATED_ORDER;
    btn.innerHTML = `Run Audit <span class="kbd">↵</span>`;
  }
}

function showError(msg) {
  $("result").classList.add("hidden");
  $("validation").classList.add("hidden");
  const empty = $("empty");
  empty.classList.remove("hidden");
  empty.innerHTML = `<div class="empty-eye">🚫</div>
    <h2>Could not complete the request</h2>
    <p>${esc(msg)}<br/><br/>
    <span class="muted">${DATA_SOURCE === "fixtures"
      ? "Set MAGENTO_DB_PASSWORD in .env for live Magento validation."
      : "Demo fixtures: " + (KNOWN.map(esc).join(", ") || "(none)")}</span></p>`;
}

function render(data) {
  const { summary, narrative, findings } = data;
  $("empty").classList.add("hidden");
  $("result").classList.remove("hidden");

  const isPass = summary.verdict === "PASS";
  const vc = $("verdictCard");
  vc.classList.toggle("pass", isPass);
  vc.classList.toggle("flag", !isPass);
  $("verdict").textContent = isPass ? "PASS" : "FLAG";
  $("orderMeta").textContent = `${summary.order_id} · ${summary.order_type}`;
  $("risk").textContent = inr(summary.rupees_at_risk);
  $("sev").textContent = summary.top_severity;
  $("count").textContent = summary.failure_count;

  $("narrative").innerHTML =
    `${esc(narrative.verdict || "")}<span class="engine">reasoner: ${esc(narrative.engine || "offline")}</span>`;

  const issues = narrative.issues || [];
  $("findingsMeta").textContent = issues.length ? `${issues.length} flagged` : "all clear";
  $("issues").innerHTML = issues.length
    ? issues.map(issueCard).join("")
    : `<div class="pass-banner">✓ All module checks passed — no revenue leakage detected.</div>`;

  const anoms = narrative.anomalies || [];
  $("anomaliesWrap").classList.toggle("hidden", anoms.length === 0);
  $("anomalies").innerHTML = anoms.map((a) => `<div class="anomaly">⚠️ ${esc(a)}</div>`).join("");

  $("rawTable").innerHTML = rawTable(findings);
  $("reasonerText").textContent = "reasoner: " + (narrative.engine || "offline");
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function issueCard(it) {
  const sev = (it.severity || "INFO").toUpperCase();
  const line = it.line ? `<div class="issue-line">${esc(it.line)}</div>` : "";
  const impact = it.rupee_impact ? `<span class="issue-impact">${inr(it.rupee_impact)}</span>` : "";
  return `<div class="issue ${sev}">
    <div class="issue-head">
      <span class="badge ${sev}">${sev}</span>
      <span class="issue-title">${esc(it.title)}</span>${impact}
    </div>${line}
    <div class="issue-detail">${esc(it.explanation || "")}</div>
    <div class="issue-fix"><b>Fix:</b> ${esc(it.fix || "")}</div>
  </div>`;
}

function rawTable(findings) {
  if (!findings || !findings.length) return "<p class='muted' style='margin-top:10px'>No failing checks.</p>";
  const rows = findings.map((f) => `<tr>
    <td>${esc(f.module)}</td><td>${esc(f.rule_id)}</td><td>${esc(f.severity)}</td>
    <td style="font-family:Inter">${esc(f.title)}</td><td>${esc(f.expected)}</td><td>${esc(f.actual)}</td>
    <td class="imp">${f.rupee_impact ? inr(f.rupee_impact) : "—"}</td></tr>`).join("");
  return `<table><thead><tr>
    <th>Module</th><th>Rule</th><th>Severity</th><th>Title</th><th>Expected</th><th>Actual</th><th>₹ Impact</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

$("validateBtn").addEventListener("click", runValidate);
$("auditBtn").addEventListener("click", runAudit);
$("order").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    if (VALIDATED_ORDER && !$("auditBtn").disabled) runAudit();
    else runValidate();
  }
});
loadHealth();
loadOrders();
