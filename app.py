"""Eagle Eye — Streamlit verdict UI.

Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.

    streamlit run app.py
"""
import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from audit.engine import audit
from audit.loader import list_order_ids
from audit.reasoner import explain

st.set_page_config(page_title="Eagle Eye · CaratLane", page_icon="🦅", layout="wide")

st.title("🦅 Eagle Eye")
st.caption("Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.")

ids = list_order_ids()
col1, col2 = st.columns([3, 1])
with col1:
    order_id = st.selectbox("Order ID", ids, index=0 if ids else None,
                            help="In production this arrives from the OIC 'order placed' webhook.")
with col2:
    st.write("")
    st.write("")
    run = st.button("🔍 Audit order", type="primary", use_container_width=True)

if run and order_id:
    with st.spinner("Running module checks…"):
        result = audit(order_id)
        narrative = explain(result)

    is_pass = result.verdict == "PASS"
    v_col, r_col, s_col = st.columns(3)
    v_col.metric("Verdict", "✅ PASS" if is_pass else "🚩 FLAG")
    r_col.metric("₹ Revenue at risk", f"₹{result.rupees_at_risk:,.0f}")
    s_col.metric("Top severity", result.top_severity)

    (st.success if is_pass else st.error)(narrative.get("verdict", ""))
    st.caption(f"reasoner: {narrative.get('engine', 'offline')} · "
               f"order type: {result.order.order_type} · "
               f"{len(result.failures)} issue(s)")

    issues = narrative.get("issues", [])
    if not issues:
        st.balloons()
    for issue in issues:
        sev = issue.get("severity", "INFO")
        badge = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "⚪"}.get(sev, "⚪")
        impact = issue.get("rupee_impact") or 0
        line = f" · `{issue['line']}`" if issue.get("line") else ""
        with st.expander(f"{badge} **[{sev}] {issue['title']}** — ₹{impact:,.2f}{line}", expanded=True):
            st.write(issue.get("explanation", ""))
            st.info(f"**Suggested fix:** {issue.get('fix', '')}")

    anomalies = narrative.get("anomalies", [])
    if anomalies:
        st.subheader("⚠️ Anomalies (AI catch-all over free-text)")
        for a in anomalies:
            st.warning(a)

    with st.expander("🔬 Raw deterministic findings (source of truth)"):
        st.dataframe([
            {"module": f.module, "rule": f.rule_id, "severity": f.severity,
             "title": f.title, "expected": f.expected, "actual": f.actual,
             "₹impact": f.rupee_impact, "line": f.line}
            for f in result.sorted_failures()
        ], use_container_width=True)

with st.sidebar:
    st.header("How it works")
    st.markdown(
        "1. **Collect** order snapshot (fixture → OIC/Fusion in prod)\n"
        "2. **Decide** — deterministic rule engine computes findings + ₹ impact\n"
        "3. **Explain** — Claude writes the auditor narrative (offline fallback if no key)\n"
        "4. **Verdict** — PASS / FLAG with rupees at risk\n\n"
        "_Code decides the money. Claude only explains — it never recomputes._"
    )
    st.divider()
    st.caption("CaratLane Hackathon · zero-cost demo")
