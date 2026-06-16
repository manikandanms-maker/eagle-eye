const $ = (id) => document.getElementById(id);
const inr = (n) => "₹" + Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let KNOWN = [];

async function loadOrders() {
  try {
    const { orders } = await (await fetch("/api/orders")).json();
    KNOWN = orders || [];
    // editable input keeps full freedom; datalist + chips give discoverability
    $("orderList").innerHTML = KNOWN.map((o) => `<option value="${esc(o)}">`).join("");
    $("chips").innerHTML =
      `<span class="muted" style="align-self:center">Try:</span>` +
      KNOWN.map((o) => `<button class="chip" data-id="${esc(o)}">${esc(o)}</button>`).join("");
    document.querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => { $("order").value = c.dataset.id; runAudit(); })
    );
    if (KNOWN.length && !$("order").value) $("order").value = KNOWN.find((o) => o.endsWith("1001")) || KNOWN[0];
  } catch (_) { /* server may still be booting */ }
}

async function runAudit() {
  const id = $("order").value.trim();
  if (!id) { $("order").focus(); return; }
  const btn = $("auditBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> Auditing…`;
  try {
    const res = await fetch(`/api/audit/${encodeURIComponent(id)}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Order not found (${res.status})`);
    }
    render(await res.json());
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `Run Audit <span class="kbd">↵</span>`;
  }
}

function showError(msg) {
  // render into the placeholder so the result DOM stays intact for the next run
  $("result").classList.add("hidden");
  const empty = $("empty");
  empty.classList.remove("hidden");
  empty.innerHTML = `<div class="empty-eye">🚫</div>
    <h2>Could not audit that order</h2>
    <p>${esc(msg)}<br/><br/><span class="muted">Known IDs: ${KNOWN.map(esc).join(", ") || "(none loaded)"}</span></p>`;
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

$("auditBtn").addEventListener("click", runAudit);
$("order").addEventListener("keydown", (e) => { if (e.key === "Enter") runAudit(); });
loadOrders();
