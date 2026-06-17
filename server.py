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
from audit.db import db_configured, DatabaseConfigError
from audit.fusion_db import fusion_db_configured
from audit.fusion_report import cache_status, refresh_report_cache, soap_configured
from audit.google_chat import webhook_configured
from audit.magento import validate_order

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="CEagle Eye", description="AI-powered order audit for CaratLane")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "magento_db": "configured" if db_configured() else "missing_password",
        "fusion_db": "configured" if fusion_db_configured() else "missing_credentials",
        "fusion_soap": "configured" if soap_configured() else "missing_credentials",
        "google_chat_webhook": "configured" if webhook_configured() else "disabled",
        "fusion_report_cache": cache_status(),
        "data_source": "magento" if db_configured() else "fixtures",
    }


@app.get("/api/orders")
def orders():
    return {"orders": list_order_ids()}


@app.get("/api/validate/{order_number}")
def validate_order_endpoint(order_number: str):
    """First-check: locate order in Magento and evaluate checklist criteria."""
    try:
        result = validate_order(order_number)
    except DatabaseConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Magento query failed: {exc}")
    if not result.found:
        raise HTTPException(status_code=404, detail=result.message)
    return result.to_dict()


@app.get("/api/fusion-report/status")
def fusion_report_status():
    return cache_status()


@app.post("/api/fusion-report/refresh")
def fusion_report_refresh():
    """Schedule Fusion BI report, download CSV, refresh ATP/WD/UOM cache (may take several minutes)."""
    try:
        return refresh_report_cache()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fusion report refresh failed: {exc}")


@app.get("/api/audit/{order_id}")
def audit_order(order_id: str, source: str = "auto"):
    try:
        result = audit(order_id, source=source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DatabaseConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Order fetch failed: {exc}")
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
