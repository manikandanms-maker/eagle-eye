const $ = (id) => document.getElementById(id);
const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const esc = (s) => {
  if (s == null) return "";
  if (typeof s === "object") {
    try { s = JSON.stringify(s); } catch (_) { s = String(s); }
  } else {
    s = String(s);
  }
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
};

let KNOWN = [];
let VALIDATED_ORDER = null;
let DATA_SOURCE = "fixtures";
let LAST_CHECKS = [];
let CHECK_FILTER = "all";

// AP finance checks — hidden from UI until required
const HIDDEN_CHECK_NAMES = new Set([
  "sop_ap_invoice_paid",
  "sop_ledger_id",
  "sop_metal_loss_values",
  "sop_wo_completion_buy",
]);

function visibleChecks(checks) {
  return (checks || []).filter((c) => !HIDDEN_CHECK_NAMES.has(c?.name));
}

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

  LAST_CHECKS = visibleChecks(data.checks || []);
  CHECK_FILTER = "all";
  renderCheckSummary(LAST_CHECKS, data.header || {});
  renderCheckFilters();
  renderChecklists(LAST_CHECKS);

  $("orderDetails").innerHTML = orderDetailsTable(data.lines || [], data.header || {}, data.customer || {});
  renderMfgReservation(data.lines || []);
  renderJwQcGrn(data.lines || []);
  renderWorkOrders(data.work_orders || []);
}

function checkStatus(c) {
  if (!c?.passed) return "fail";
  const action = String(c?.action || "").trim();
  if (action && action !== "Not implemented") return "warn";
  return "pass";
}

function summarizeChecks(checks) {
  let pass = 0, fail = 0, warn = 0;
  for (const c of checks) {
    const s = checkStatus(c);
    if (s === "fail") fail++;
    else if (s === "warn") warn++;
    else pass++;
  }
  return { pass, fail, warn, total: checks.length };
}

function renderCheckSummary(checks, header) {
  const el = $("checkSummary");
  if (!el || !checks.length) {
    if (el) el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const { pass, fail, warn, total } = summarizeChecks(checks);
  const pct = total ? Math.round((pass / total) * 100) : 0;
  const net = header?.net_payable;
  const highVal = Number(net) > 500000;
  const orderVal = net != null ? inr(net) : null;

  el.innerHTML = `
    <div class="summary-stats">
      <span class="stat-pill pass">✓ ${pass} Passed</span>
      <span class="stat-pill fail">✗ ${fail} Failed</span>
      <span class="stat-pill warn">⚠ ${warn} Warning</span>
      ${orderVal ? `<span class="stat-pill${highVal ? " warn" : ""}">Order ${orderVal}${highVal ? " · HIGH VALUE" : ""}</span>` : ""}
    </div>
    <div class="progress-wrap">
      <div class="progress-bar">
        ${pass ? `<div class="progress-seg pass" style="width:${(pass/total)*100}%"></div>` : ""}
        ${warn ? `<div class="progress-seg warn" style="width:${(warn/total)*100}%"></div>` : ""}
        ${fail ? `<div class="progress-seg fail" style="width:${(fail/total)*100}%"></div>` : ""}
      </div>
      <div class="progress-label">${pass}/${total} passed (${pct}%) · ${fail} failed · ${warn} warnings</div>
    </div>`;
}

function renderCheckFilters() {
  const el = $("checkFilters");
  if (!el) return;
  if (!LAST_CHECKS.length) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const filters = [
    ["all", "All"],
    ["issues", "Issues only"],
    ["fail", "Failed"],
    ["warn", "Warnings"],
    ["pass", "Passed"],
  ];
  el.innerHTML = filters.map(([id, label]) =>
    `<button type="button" class="filter-btn${id === "issues" ? " issues" : ""}${CHECK_FILTER === id ? " active" : ""}" data-filter="${id}">${label}</button>`
  ).join("");
  el.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      CHECK_FILTER = btn.dataset.filter;
      renderCheckFilters();
      applyCheckFilter();
    });
  });
}

function itemMatchesFilter(status) {
  if (CHECK_FILTER === "all") return true;
  if (CHECK_FILTER === "issues") return status === "fail" || status === "warn";
  return status === CHECK_FILTER;
}

function applyCheckFilter() {
  document.querySelectorAll(".check-item").forEach((row) => {
    const status = row.dataset.status || "pass";
    row.classList.toggle("hidden-filter", !itemMatchesFilter(status));
  });
  document.querySelectorAll(".check-section").forEach((sec) => {
    const visible = sec.querySelectorAll(".check-item:not(.hidden-filter)").length;
    sec.classList.toggle("hidden", visible === 0);
    if (CHECK_FILTER !== "all" && visible > 0) sec.setAttribute("open", "");
  });
}

function sortChecks(checks) {
  const rank = { fail: 0, warn: 1, pass: 2 };
  return [...checks].sort((a, b) => {
    const ra = rank[checkStatus(a)] ?? 9;
    const rb = rank[checkStatus(b)] ?? 9;
    return ra - rb || String(a.label).localeCompare(String(b.label));
  });
}

function renderDetailTable(rows, cols, { rowClassFn } = {}) {
  if (!rows.length) return "";
  const body = rows.map((r) => {
    const cls = rowClassFn ? rowClassFn(r) : "";
    return `<tr class="${cls}">${cols.map((c) => {
      const cell = typeof c.render === "function" ? c.render(r) : cellVal(r, c.key || c);
      return `<td>${cell}</td>`;
    }).join("")}</tr>`;
  }).join("");
  const heads = cols.map((c) => `<th>${esc((c.label || String(c.key || c)).replace(/_/g, " "))}</th>`).join("");
  return `<div class="table-scroll"><table><thead><tr>${heads}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function isFusionReserved(v) {
  if (v === true) return true;
  const s = String(v ?? "").trim().toLowerCase();
  return s === "1" || s === "true" || s === "yes";
}

const MFG_LINE_TYPES = new Set(["Inhouse", "JW", "Make"]);
const GRN_OK = new Set(["DONE", "COMPLETE", "COMPLETED", "RECEIVED", "CLOSED"]);

function renderMfgReservation(lines) {
  const wrap = $("mfgReservationWrap");
  const el = $("mfgReservation");
  const meta = $("mfgReservationMeta");
  const rows = (lines || []).filter((ln) => MFG_LINE_TYPES.has(ln.manufacturing_type));
  if (!rows.length) {
    wrap.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const issues = rows.filter((ln) => !isFusionReserved(ln.barcode_reserved_in_fusion));
  const sorted = [...rows].sort((a, b) => {
    const ai = isFusionReserved(a.barcode_reserved_in_fusion) ? 1 : 0;
    const bi = isFusionReserved(b.barcode_reserved_in_fusion) ? 1 : 0;
    return ai - bi || String(a.barcode).localeCompare(String(b.barcode));
  });
  wrap.classList.remove("hidden");
  wrap.classList.toggle("has-issues", issues.length > 0);
  if (issues.length) wrap.setAttribute("open", "");
  else wrap.removeAttribute("open");
  meta.textContent = `${rows.length} line(s) · ${issues.length} not reserved`;

  const cols = [
    { key: "barcode" },
    { key: "item_number" },
    { key: "manufacturing_type", label: "mfg type" },
    { key: "erp_status" },
    { key: "barcode_reserved_in_fusion", label: "reserved in fusion", render: (r) => isFusionReserved(r.barcode_reserved_in_fusion) ? "YES" : "NO" },
    { key: "barcode_reservation_id", label: "reservation id" },
    {
      label: "status",
      render: (r) => isFusionReserved(r.barcode_reserved_in_fusion)
        ? '<span class="stat-pill pass" style="padding:2px 8px;font-size:11px">Reserved</span>'
        : '<span class="stat-pill fail" style="padding:2px 8px;font-size:11px">Not reserved</span>',
    },
  ];
  el.innerHTML = `<p class="detail-section-hint">Make / Inhouse / JW barcodes must have <code>barcode_reserved_in_fusion = 1</code> before fulfillment.</p>`
    + renderDetailTable(sorted, cols, {
      rowClassFn: (r) => (isFusionReserved(r.barcode_reserved_in_fusion) ? "row-ok" : "row-issue"),
    });
}

function jwQcReject(ln) {
  const qc = String(ln.vendor_qc_status ?? "").trim().toLowerCase();
  return qc && qc.includes("reject");
}

function jwGrnIssue(ln) {
  if (ln.manufacturing_type !== "JW") return false;
  const grn = String(ln.grn_status ?? "").trim().toUpperCase();
  return grn && !GRN_OK.has(grn);
}

function renderJwQcGrn(lines) {
  const wrap = $("jwQcGrnWrap");
  const el = $("jwQcGrn");
  const meta = $("jwQcGrnMeta");
  const rows = (lines || []).filter((ln) => ln.manufacturing_type === "JW");
  if (!rows.length) {
    wrap.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const issues = rows.filter((ln) => jwQcReject(ln) || jwGrnIssue(ln));
  const sorted = [...rows].sort((a, b) => {
    const ai = jwQcReject(a) || jwGrnIssue(a) ? 0 : 1;
    const bi = jwQcReject(b) || jwGrnIssue(b) ? 0 : 1;
    return ai - bi || String(a.barcode).localeCompare(String(b.barcode));
  });
  wrap.classList.remove("hidden");
  wrap.classList.toggle("has-issues", issues.length > 0);
  if (issues.length) wrap.setAttribute("open", "");
  else wrap.removeAttribute("open");
  meta.textContent = `${rows.length} JW line(s) · ${issues.length} with QC/GRN issues`;

  const cols = [
    { key: "barcode" },
    { key: "item_number" },
    { key: "erp_status" },
    { key: "po_number" },
    { key: "grn_transaction_number", label: "grn po / txn" },
    { key: "grn_status" },
    { key: "grn_gross_weight" },
    { key: "vendor_qc_status", label: "vendor QC" },
    { key: "vendor_qc_status_time", label: "QC time" },
    {
      label: "status",
      render: (r) => {
        if (jwQcReject(r)) return '<span class="stat-pill fail" style="padding:2px 8px;font-size:11px">QC reject</span>';
        if (jwGrnIssue(r)) return '<span class="stat-pill fail" style="padding:2px 8px;font-size:11px">GRN pending</span>';
        return '<span class="stat-pill pass" style="padding:2px 8px;font-size:11px">OK</span>';
      },
    },
  ];
  el.innerHTML = `<p class="detail-section-hint">JW lines: Vendor QC App status + Fusion GRN. Failures drive the SOP QC / GRN check.</p>`
    + renderDetailTable(sorted, cols, {
      rowClassFn: (r) => (jwQcReject(r) || jwGrnIssue(r) ? "row-issue" : "row-ok"),
    });
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
    // "wo_completion_status", — disabled until Fusion actual-completion signal is wired
    "invoice_number",
    // "ap_payment_status", "ap_ledger_id", "ap_balance_amount", — not required in UI yet
    "rm_consumed", "rm_row_count",
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
    if (integrations.saas_finance) bits.push("SaaS finance (AR/AP)");
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

// Render one section per module present in the response. Known modules come first
// (in this order); any new module the backend adds appears automatically after them.
const MODULE_ORDER = ["ORDER", "CUSTOMER", "BARCODE", "PRICING", "SOP", "INVOICE", "PROCUREMENT", "MANUFACTURING"];
const MODULE_LABEL = {
  ORDER: "Order checks", CUSTOMER: "Customer checks",
  BARCODE: "Barcode / Fusion checks", PRICING: "Pricing checks",
  SOP: "SOP / Escalation checks",
  INVOICE: "Invoice checks", PROCUREMENT: "Procurement checks",
  MANUFACTURING: "Manufacturing checks",
};

function moduleLabel(m) {
  return MODULE_LABEL[m] || (m ? m.charAt(0) + m.slice(1).toLowerCase() + " checks" : "Other checks");
}

function renderChecklists(checks) {
  const el = $("checklists");
  if (!el) return;
  const safe = Array.isArray(checks) ? checks : [];
  if (!safe.length) {
    el.innerHTML = `<p class="muted" style="padding:8px 2px">No checks returned.</p>`;
    return;
  }

  const groups = {};
  for (const c of safe) {
    const mod = c?.module || "OTHER";
    (groups[mod] = groups[mod] || []).push(c);
  }

  const rank = (m) => { const i = MODULE_ORDER.indexOf(m); return i < 0 ? 99 : i; };
  const modules = Object.keys(groups).sort((a, b) => {
    const aIssue = groups[a].some((c) => checkStatus(c) !== "pass");
    const bIssue = groups[b].some((c) => checkStatus(c) !== "pass");
    if (aIssue !== bIssue) return aIssue ? -1 : 1;
    return rank(a) - rank(b) || a.localeCompare(b);
  });

  el.innerHTML = modules.map((m, idx) => {
    const items = sortChecks(groups[m]);
    const passed = items.filter((c) => c.passed).length;
    const hasFail = items.some((c) => !c.passed);
    const hasIssue = items.some((c) => checkStatus(c) !== "pass");
    const allPass = passed === items.length;
    const open = hasIssue;
    const countCls = hasFail ? "section-count has-fail" : "section-count";
    return `<details class="check-section${hasFail ? " has-fail" : allPass ? " all-pass" : ""}" data-module="${esc(m)}" ${open ? "open" : ""}>
      <summary>
        <span>${esc(moduleLabel(m))}</span>
        <span class="${countCls}">${passed}/${items.length} passed</span>
      </summary>
      <div class="checklist">${items.map((c, i) => renderCheckItem(c, `${m}-${i}`)).join("")}</div>
    </details>`;
  }).join("");

  el.querySelectorAll(".check-details-toggle").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const id = btn.dataset.target;
      const panel = document.getElementById(id);
      if (!panel) return;
      const hidden = panel.classList.toggle("hidden");
      btn.textContent = hidden ? "Show details" : "Hide details";
    });
  });

  applyCheckFilter();
}

function renderCheckItem(c, id) {
  const status = checkStatus(c);
  const label = c?.label || c?.name || "Check";
  const actual = c?.actual ?? "";
  const expected = c?.expected ?? "";
  const detail = c?.detail ?? "";
  const action = c?.action ?? "";
  const badge = status;
  const badgeLabel = status === "fail" ? "Fail" : status === "warn" ? "Warn" : "Pass";
  const showValue = actual && (status !== "pass" || String(actual).length <= 48);
  const showDetails = expected || detail || (status === "fail" && action);
  const detailsId = `chk-${id}`;

  return `<div class="check-item ${status === "fail" ? "bad" : status === "warn" ? "warn" : "ok"}" data-status="${badge}">
    <div class="check-row">
      <div class="check-title-wrap">
        <span class="check-title">${esc(label)}</span>
      </div>
      <span class="check-badge ${badge}">${badgeLabel}</span>
    </div>
    ${showValue ? `<div class="check-value" title="${esc(actual)}">${esc(actual)}</div>` : ""}
    ${status !== "pass" && action ? `<div class="check-action">➜ ${esc(action)}</div>` : ""}
    ${showDetails ? `<button type="button" class="check-details-toggle" data-target="${detailsId}">${status === "fail" ? "Hide details" : "Show details"}</button>
    <div id="${detailsId}" class="check-details${status === "fail" ? "" : " hidden"}">
      ${expected ? `<div><span class="muted">Expected:</span> ${esc(expected)}</div>` : ""}
      ${detail ? `<div>${esc(detail)}</div>` : ""}
    </div>` : ""}
  </div>`;
}

function cellVal(ln, key) {
  const v = ln[key];
  if (v == null || v === "") return "—";
  if (typeof v === "boolean") return v ? "YES" : "NO";
  if (key === "duplicate_onhand_count" && (v === 0 || v === "0")) return "0";
  if (key === "final_price" || key === "price_before_tax" || key === "tax" || key === "loss_stock_value") {
    const n = Number(v);
    return Number.isFinite(n) ? inr(n) : esc(v);
  }
  return esc(v);
}

const JW_LINE_COLS = new Set([
  "po_number", "grn_transaction_number", "grn_status", "grn_gross_weight",
  "vendor_qc_status", "vendor_qc_status_time",
]);
const INHOUSE_LINE_COLS = new Set(["bag_status", "factory", "loss_weight", "loss_stock_value"]);
const AR_LINE_COLS = new Set(["ar_status", "ar_amount_due_remaining"]);

function lineCellVal(ln, key) {
  const mfg = ln.manufacturing_type;
  if (JW_LINE_COLS.has(key) && mfg !== "JW") return "N/A";
  if (INHOUSE_LINE_COLS.has(key) && mfg !== "Inhouse") return "N/A";
  if (AR_LINE_COLS.has(key) && !ln.invoice_number) return "N/A";
  return cellVal(ln, key);
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
    "invoice_number", "invoice_flag",
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
  const hasInvoice = lines.some((ln) => ln.invoice_number);
  if (hasInvoice) {
    extraCols.push("ar_status", "ar_amount_due_remaining");
  }
  if (hasJw) {
    extraCols.push(
      "po_number",
      "grn_transaction_number", "grn_status", "grn_gross_weight",
      "vendor_qc_status", "vendor_qc_status_time"
    );
  }
  const cols = [...baseCols, ...extraCols];

  const rows = lines.map((ln) =>
    `<tr>${cols.map((c) => `<td>${lineCellVal(ln, c)}</td>`).join("")}</tr>`
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
