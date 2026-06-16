"""Eagle Eye web server — FastAPI backend + static SPA, one process, one port.

Eagle Eye — Watching Every Transaction. Predicting Every Risk. Protecting Every Fulfillment.

    pip install -r requirements.txt
    python server.py            # -> http://127.0.0.1:8000

Endpoints:
    GET  /                      -> the single-page UI (web/index.html)
    GET  /api/orders           -> list available order IDs
    GET  /api/audit/{order_id} -> full audit result + AI narrative
"""
from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from audit.engine import audit
from audit.loader import list_order_ids
from audit.reasoner import explain

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Eagle Eye", description="AI-powered order audit for CaratLane")


@app.get("/api/orders")
def orders():
    return {"orders": list_order_ids()}


@app.get("/api/audit/{order_id}")
def audit_order(order_id: str):
    try:
        result = audit(order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    narrative = explain(result)
    return {
        "summary": result.summary(),
        "narrative": narrative,
        "findings": [
            {
                "module": f.module, "rule_id": f.rule_id, "severity": f.severity,
                "title": f.title, "expected": f.expected, "actual": f.actual,
                "rupee_impact": f.rupee_impact, "line": f.line, "detail": f.detail,
            }
            for f in result.sorted_failures()
        ],
    }


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


# serve css/js
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
