"""Fusion BI Publisher SOAP — schedule, download, cache, and item ATP/WD/UOM lookup.

Ports the Node fetchFusionReportScheduled flow for:
  /Custom/Extraction Reports/ITEM ATP WD UOM STATUS/item_wd_atp_uom.xdo

Order validation uses a local CSV cache for fast item_number lookups.
Refresh the cache via POST /api/fusion-report/refresh or:
  python -m audit.fusion_report --refresh
"""
from __future__ import annotations

import base64
import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

V2_NS = "http://xmlns.oracle.com/oxp/service/v2"
PUB_NS = "http://xmlns.oracle.com/oxp/service/PublicReportService"
DEFAULT_EXTERNAL_SOAP_URL = (
    "https://iaaycp.fa.ocs.oraclecloud.com/xmlpserver/services/ExternalReportWSSService"
)
DEFAULT_SCHEDULE_SOAP_URL = (
    "https://iaaycp.fa.ocs.oraclecloud.com/xmlpserver/services/v2/ScheduleService"
)
DEFAULT_REPORT_PATH = (
    "/Custom/Extraction Reports/ITEM ATP WD UOM STATUS/item_wd_atp_uom.xdo"
)

TERMINAL_JOB_STATUSES = frozenset({
    "SUCCESS", "SUCCEEDED", "COMPLETE", "COMPLETED", "ERROR", "FAILED",
    "WARNING", "CANCELLED", "CANCELED",
})

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "fusion_reports"
CACHE_CSV = CACHE_DIR / "item_wd_atp_uom.csv"
CACHE_META = CACHE_DIR / "item_wd_atp_uom.meta.json"

_refresh_lock = False
_item_status_cache: dict[str, tuple[float, dict]] = {}


class FusionSoapConfigError(RuntimeError):
    pass


def soap_configured() -> bool:
    return bool(_user() and _password())


def _user() -> str:
    return os.getenv("FUSION_SOAP_USER", "").strip()


def _password() -> str:
    return os.getenv("FUSION_SOAP_PASSWORD", "").strip()


def _report_path() -> str:
    return os.getenv("FUSION_REPORT_PATH", DEFAULT_REPORT_PATH).strip()


def _cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("FUSION_REPORT_CACHE_TTL", "21600"))  # 6h default
    except ValueError:
        return 21600


def _item_param_name() -> str:
    return os.getenv("FUSION_REPORT_ITEM_PARAM", "item_number").strip() or "item_number"


def _run_report_timeout() -> int:
    try:
        return int(os.getenv("FUSION_RUN_REPORT_TIMEOUT", "180"))
    except ValueError:
        return 180


def escape_xml(value: Any) -> str:
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def extract_xml_element_text(xml: str, local_name: str) -> Optional[str]:
    pattern = re.compile(
        rf"<(?:[^:>]+:)?{re.escape(local_name)}[^>]*>([\s\S]*?)</(?:[^:>]+:)?{re.escape(local_name)}>",
        re.IGNORECASE,
    )
    match = pattern.search(xml or "")
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() == "null" or re.match(r"^xsi:nil", value, re.I):
        return None
    return value


def extract_soap_fault(xml: str) -> None:
    if not xml or "Fault" not in xml:
        return
    fault = extract_xml_element_text(xml, "faultstring") or extract_xml_element_text(xml, "Reason")
    if fault:
        raise RuntimeError(f"Fusion SOAP fault: {fault}")


def extract_job_instance_ids(xml: str) -> list[str]:
    from_return = re.findall(
        r"<(?:[^:>]+:)?getAllJobInstanceIDsReturn[^>]*>([\s\S]*?)</(?:[^:>]+:)?getAllJobInstanceIDsReturn>",
        xml or "",
        re.I,
    )
    if from_return:
        nested: list[str] = []
        for block in from_return:
            for item in re.findall(r"<(?:[^:>]+:)?item[^>]*>([^<]+)</", block, re.I):
                item = item.strip()
                if item:
                    nested.append(item)
        if nested:
            return nested
    ids = re.findall(r"<(?:[^:>]+:)?item[^>]*>([^<]+)</", xml or "", re.I)
    return [i.strip() for i in ids if i.strip()]


def _resolve_schedule_soap_url() -> str:
    return os.getenv("FUSION_SCHEDULE_SOAP_URL", DEFAULT_SCHEDULE_SOAP_URL).strip()


def _resolve_external_soap_url() -> str:
    return os.getenv("FUSION_SOAP_URL", DEFAULT_EXTERNAL_SOAP_URL).strip()


def post_fusion_soap(
    url: str,
    envelope: str,
    *,
    use_body_credentials: bool = False,
    timeout: int = 120,
    soap_version: str = "1.1",
) -> str:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is required for Fusion SOAP. pip install requests") from exc

    if soap_version == "1.2":
        headers = {"Content-Type": "application/soap+xml; charset=utf-8", "SOAPAction": '""'}
        auth = (_user(), _password())
    else:
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'}
        auth = None if use_body_credentials else (_user(), _password())
    resp = requests.post(
        url,
        data=envelope.encode("utf-8"),
        headers=headers,
        auth=auth,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def build_schedule_report_envelope(
    report_path: str,
    attribute_format: str,
    user_job_name: str,
    user_id: str,
    password: str,
    start_date: Optional[str] = None,
) -> str:
    start_xml = f"<v2:startDate>{escape_xml(start_date)}</v2:startDate>" if start_date else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v2="{V2_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <v2:scheduleReport>
      <v2:scheduleRequest>
        <v2:jobLocale>en-US</v2:jobLocale>
        <v2:jobTZ>Asia/Calcutta</v2:jobTZ>
        <v2:mergeOutputOption>false</v2:mergeOutputOption>
        <v2:notifyWhenFailed>false</v2:notifyWhenFailed>
        <v2:notifyWhenSkipped>false</v2:notifyWhenSkipped>
        <v2:notifyWhenSuccess>false</v2:notifyWhenSuccess>
        <v2:notifyWhenWarning>false</v2:notifyWhenWarning>
        <v2:repeatCount>1</v2:repeatCount>
        <v2:repeatInterval>0</v2:repeatInterval>
        <v2:reportRequest>
          <v2:attributeFormat>{escape_xml(attribute_format)}</v2:attributeFormat>
          <v2:attributeLocale>en-US</v2:attributeLocale>
          <v2:flattenXML>false</v2:flattenXML>
          <v2:byPassCache>true</v2:byPassCache>
          <v2:reportAbsolutePath>{escape_xml(report_path)}</v2:reportAbsolutePath>
          <v2:sizeOfDataChunkDownload>-1</v2:sizeOfDataChunkDownload>
        </v2:reportRequest>
        <v2:saveDataOption>true</v2:saveDataOption>
        <v2:saveOutputOption>true</v2:saveOutputOption>
        <v2:schedulePublicOption>true</v2:schedulePublicOption>
        {start_xml}
        <v2:userJobName>{escape_xml(user_job_name)}</v2:userJobName>
      </v2:scheduleRequest>
      <v2:userID>{escape_xml(user_id)}</v2:userID>
      <v2:password>{escape_xml(password)}</v2:password>
    </v2:scheduleReport>
  </soapenv:Body>
</soapenv:Envelope>"""


def build_get_all_job_instance_ids_envelope(parent_job_id: str, user_id: str, password: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v2="{V2_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <v2:getAllJobInstanceIDs>
      <v2:submittedJobId>{escape_xml(parent_job_id)}</v2:submittedJobId>
      <v2:userID>{escape_xml(user_id)}</v2:userID>
      <v2:password>{escape_xml(password)}</v2:password>
    </v2:getAllJobInstanceIDs>
  </soapenv:Body>
</soapenv:Envelope>"""


def build_get_scheduled_report_status_envelope(job_id: str, user_id: str, password: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v2="{V2_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <v2:getScheduledReportStatus>
      <v2:scheduledJobID>{escape_xml(job_id)}</v2:scheduledJobID>
      <v2:userID>{escape_xml(user_id)}</v2:userID>
      <v2:password>{escape_xml(password)}</v2:password>
    </v2:getScheduledReportStatus>
  </soapenv:Body>
</soapenv:Envelope>"""


def build_get_scheduled_report_output_info_envelope(job_id: str, user_id: str, password: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v2="{V2_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <v2:getScheduledReportOutputInfo>
      <v2:jobInstanceID>{escape_xml(job_id)}</v2:jobInstanceID>
      <v2:userID>{escape_xml(user_id)}</v2:userID>
      <v2:password>{escape_xml(password)}</v2:password>
    </v2:getScheduledReportOutputInfo>
  </soapenv:Body>
</soapenv:Envelope>"""


def build_download_document_data_envelope(job_output_id: str, user_id: str, password: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:v2="{V2_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <v2:downloadDocumentData>
      <v2:jobOutputID>{escape_xml(job_output_id)}</v2:jobOutputID>
      <v2:userID>{escape_xml(user_id)}</v2:userID>
      <v2:password>{escape_xml(password)}</v2:password>
    </v2:downloadDocumentData>
  </soapenv:Body>
</soapenv:Envelope>"""


def build_run_report_envelope(
    report_path: str,
    item_number: str,
    user_id: str,
    password: str,
    *,
    attribute_format: str = "csv",
    param_name: Optional[str] = None,
) -> str:
    pname = param_name or _item_param_name()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope" xmlns:pub="{PUB_NS}">
  <soap12:Header/>
  <soap12:Body>
    <pub:runReport>
      <pub:reportRequest>
        <pub:attributeFormat>{escape_xml(attribute_format)}</pub:attributeFormat>
        <pub:attributeLocale>en-US</pub:attributeLocale>
        <pub:byPassCache>true</pub:byPassCache>
        <pub:reportAbsolutePath>{escape_xml(report_path)}</pub:reportAbsolutePath>
        <pub:sizeOfDataChunkDownload>-1</pub:sizeOfDataChunkDownload>
        <pub:parameterNameValues>
          <pub:item>
            <pub:name>{escape_xml(pname)}</pub:name>
            <pub:values>
              <pub:item>{escape_xml(item_number)}</pub:item>
            </pub:values>
          </pub:item>
        </pub:parameterNameValues>
      </pub:reportRequest>
      <pub:userID>{escape_xml(user_id)}</pub:userID>
      <pub:password>{escape_xml(password)}</pub:password>
    </pub:runReport>
  </soap12:Body>
</soap12:Envelope>"""


def build_download_report_data_chunk_envelope(file_id: str, begin_idx: int, size: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:pub="{PUB_NS}">
  <soapenv:Header/>
  <soapenv:Body>
    <pub:downloadReportDataChunk>
      <pub:reportFileID>{escape_xml(file_id)}</pub:reportFileID>
      <pub:beginIdx>{begin_idx}</pub:beginIdx>
      <pub:size>{size}</pub:size>
    </pub:downloadReportDataChunk>
  </soapenv:Body>
</soapenv:Envelope>"""


def _normalize_job_status(status: Optional[str]) -> str:
    return str(status or "").strip().upper()


def _is_terminal_job_status(status: Optional[str]) -> bool:
    normalized = _normalize_job_status(status)
    return normalized in TERMINAL_JOB_STATUSES or "SUCCESS" in normalized


def _is_successful_job_status(status: Optional[str]) -> bool:
    return "SUCCESS" in _normalize_job_status(status)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _fetch_scheduled_job_status(schedule_url: str, job_id: str) -> dict:
    xml = post_fusion_soap(
        schedule_url,
        build_get_scheduled_report_status_envelope(job_id, _user(), _password()),
        use_body_credentials=True,
    )
    extract_soap_fault(xml)
    return {
        "jobStatus": extract_xml_element_text(xml, "jobStatus") or extract_xml_element_text(xml, "status"),
        "statusDetail": extract_xml_element_text(xml, "statusDetail") or extract_xml_element_text(xml, "message"),
    }


def _wait_for_parent_job_to_leave_scheduled(schedule_url: str, parent_job_id: str) -> str:
    poll_ms = int(os.getenv("FUSION_SCHEDULE_PARENT_POLL_MS", "15000"))
    max_wait_ms = int(os.getenv("FUSION_SCHEDULE_MAX_WAIT_MS", "3600000"))
    started = time.time()
    while (time.time() - started) * 1000 < max_wait_ms:
        status = _fetch_scheduled_job_status(schedule_url, parent_job_id).get("jobStatus")
        if status and not re.match(r"^scheduled$", status, re.I):
            logger.info("Fusion parent job %s status: %s", parent_job_id, status)
            return status
        _sleep(poll_ms / 1000)
    raise RuntimeError(f"Fusion parent job {parent_job_id} stayed Scheduled too long.")


def _wait_for_child_job_id(schedule_url: str, parent_job_id: str) -> str:
    initial_wait_ms = int(os.getenv("FUSION_SCHEDULE_CHILD_JOB_WAIT_MS", "20000"))
    retry_delay_ms = int(os.getenv("FUSION_SCHEDULE_CHILD_JOB_RETRY_MS", "10000"))
    max_attempts = int(os.getenv("FUSION_SCHEDULE_CHILD_JOB_ATTEMPTS", "60"))

    _wait_for_parent_job_to_leave_scheduled(schedule_url, parent_job_id)
    _sleep(initial_wait_ms / 1000)

    for attempt in range(1, max_attempts + 1):
        try:
            xml = post_fusion_soap(
                schedule_url,
                build_get_all_job_instance_ids_envelope(parent_job_id, _user(), _password()),
                use_body_credentials=True,
            )
            extract_soap_fault(xml)
            instance_ids = extract_job_instance_ids(xml)
            child = next((i for i in instance_ids if str(i) != str(parent_job_id)), None)
            child = child or (instance_ids[0] if instance_ids else None)
            if child:
                logger.info("Fusion child job ID: %s (parent %s)", child, parent_job_id)
                return child
        except Exception as exc:
            body = str(exc)
            if "500" not in body and "not found" not in body.lower():
                raise
            logger.info("Child job not ready yet for parent %s: %s", parent_job_id, exc)
        logger.info("Waiting for Fusion child job (attempt %s/%s)...", attempt, max_attempts)
        _sleep(retry_delay_ms / 1000)
    raise RuntimeError(f"Timed out waiting for Fusion child job ID (parent {parent_job_id}).")


def _wait_for_job_completion(schedule_url: str, child_job_id: str, parent_job_id: str) -> str:
    poll_ms = int(os.getenv("FUSION_SCHEDULE_POLL_INTERVAL_MS", "30000"))
    max_wait_ms = int(os.getenv("FUSION_SCHEDULE_MAX_WAIT_MS", "3600000"))
    started = time.time()

    while (time.time() - started) * 1000 < max_wait_ms:
        child = _fetch_scheduled_job_status(schedule_url, child_job_id)
        job_status = child.get("jobStatus")
        status_detail = child.get("statusDetail")
        polled = child_job_id

        if not job_status and parent_job_id:
            parent = _fetch_scheduled_job_status(schedule_url, parent_job_id)
            job_status = parent.get("jobStatus")
            status_detail = parent.get("statusDetail")
            polled = parent_job_id

        if job_status:
            logger.info("Fusion job %s status: %s", polled, job_status)

        if _is_terminal_job_status(job_status):
            if not _is_successful_job_status(job_status):
                raise RuntimeError(
                    f"Fusion scheduled report failed ({job_status}): {status_detail or 'no detail'}"
                )
            return str(job_status)

        _sleep(poll_ms / 1000)

    raise RuntimeError(f"Fusion scheduled report timed out after {max_wait_ms}ms.")


def _parse_download_chunk_response(xml: str) -> dict:
    offset_raw = extract_xml_element_text(xml, "reportDataOffset")
    try:
        offset = int(offset_raw) if offset_raw is not None else None
    except ValueError:
        offset = None
    return {
        "reportDataChunk": extract_xml_element_text(xml, "reportDataChunk"),
        "reportDataOffset": offset,
        "reportDataFileID": extract_xml_element_text(xml, "reportDataFileID"),
    }


def _download_fusion_file_in_chunks(external_url: str, file_id: str, output_path: Path) -> int:
    chunk_bytes = int(os.getenv("FUSION_DOWNLOAD_CHUNK_BYTES", str(5 * 1024 * 1024)))
    max_chunks = int(os.getenv("FUSION_MAX_DOWNLOAD_CHUNKS", "100000"))
    begin_idx = int(os.getenv("FUSION_DOWNLOAD_BEGIN_IDX", "0"))
    active_file_id = file_id
    total_bytes = 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as out:
        for chunk_count in range(1, max_chunks + 1):
            xml = post_fusion_soap(
                external_url,
                build_download_report_data_chunk_envelope(active_file_id, begin_idx, chunk_bytes),
                timeout=300,
            )
            extract_soap_fault(xml)
            chunk = _parse_download_chunk_response(xml)
            if chunk.get("reportDataFileID"):
                active_file_id = chunk["reportDataFileID"]
            data = chunk.get("reportDataChunk")
            if data:
                raw = re.sub(r"\s+", "", data)
                buffer = base64.b64decode(raw)
                out.write(buffer)
                total_bytes += len(buffer)
            offset = chunk.get("reportDataOffset")
            if offset == -1:
                break
            if offset is None:
                raise RuntimeError("Fusion downloadReportDataChunk returned invalid reportDataOffset.")
            begin_idx = offset
        else:
            raise RuntimeError(f"Fusion chunked download exceeded max chunks ({max_chunks}).")

    logger.info("Fusion download complete: %s bytes -> %s", total_bytes, output_path)
    return total_bytes


def fetch_fusion_report_scheduled(force: bool = False) -> Path:
    """Schedule report, poll, download CSV to cache path."""
    global _refresh_lock
    if not soap_configured():
        raise FusionSoapConfigError("Set FUSION_SOAP_USER and FUSION_SOAP_PASSWORD in .env")

    if _refresh_lock and not force:
        raise RuntimeError("Fusion report refresh already in progress.")

    schedule_url = _resolve_schedule_soap_url()
    external_url = _resolve_external_soap_url()
    report_path = _report_path()
    fmt = os.getenv("FUSION_REPORT_FORMAT", "csv").lower()
    user = _user()
    password = _password()
    job_name = os.getenv(
        "FUSION_SCHEDULE_USER_JOB_NAME",
        f"eagle-eye-atp-wd-uom-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
    )

    _refresh_lock = True
    try:
        logger.info("Submitting Fusion scheduled report: %s", report_path)
        schedule_xml = post_fusion_soap(
            schedule_url,
            build_schedule_report_envelope(report_path, fmt, job_name, user, password),
            use_body_credentials=True,
            timeout=180,
        )
        extract_soap_fault(schedule_xml)
        parent_job_id = (
            extract_xml_element_text(schedule_xml, "scheduleReportReturn")
            or extract_xml_element_text(schedule_xml, "return")
        )
        if not parent_job_id:
            raise RuntimeError("Fusion scheduleReport returned no parent job ID.")

        child_job_id = _wait_for_child_job_id(schedule_url, parent_job_id)
        _wait_for_job_completion(schedule_url, child_job_id, parent_job_id)

        output_xml = post_fusion_soap(
            schedule_url,
            build_get_scheduled_report_output_info_envelope(child_job_id, user, password),
            use_body_credentials=True,
        )
        extract_soap_fault(output_xml)
        job_output_id = extract_xml_element_text(output_xml, "outputId")
        if not job_output_id:
            raise RuntimeError(f"Fusion job {child_job_id} completed but no outputId returned.")

        doc_xml = post_fusion_soap(
            schedule_url,
            build_download_document_data_envelope(job_output_id, user, password),
            use_body_credentials=True,
        )
        extract_soap_fault(doc_xml)
        document_file_id = (
            extract_xml_element_text(doc_xml, "downloadDocumentDataReturn")
            or extract_xml_element_text(doc_xml, "reportFileID")
        )
        if not document_file_id:
            raise RuntimeError("downloadDocumentData returned no file ID.")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw_path = CACHE_CSV.with_suffix(".csv.raw")
        _download_fusion_file_in_chunks(external_url, document_file_id, raw_path)
        raw_path.replace(CACHE_CSV)

        meta = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "report_path": report_path,
            "parent_job_id": parent_job_id,
            "child_job_id": child_job_id,
            "job_output_id": job_output_id,
        }
        CACHE_META.write_text(json.dumps(meta, indent=2))
        return CACHE_CSV
    finally:
        _refresh_lock = False


def _cache_is_fresh() -> bool:
    if not CACHE_CSV.is_file() or not CACHE_META.is_file():
        return False
    try:
        meta = json.loads(CACHE_META.read_text())
        fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds()
        return age < _cache_ttl_seconds()
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return CACHE_CSV.stat().st_mtime + _cache_ttl_seconds() > time.time()


def _norm_col(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper()).strip("_")


def _pick_column(cols: dict[str, str], *candidates: str) -> Optional[str]:
    for c in candidates:
        key = _norm_col(c)
        if key in cols:
            return cols[key]
    for key, val in cols.items():
        for c in candidates:
            if c in key:
                return val
    return None


def _row_to_status(norm: dict[str, str], fallback_item: Optional[str] = None) -> Optional[dict]:
    item = _pick_column(norm, "ITEM_NUMBER", "ITEM_NO", "ITEM") or fallback_item
    if not item:
        return None
    return {
        "item_number": item,
        "atp_status": _pick_column(norm, "ATP_STATUS", "ATP", "ITEM_ATP", "ATP_FLAG"),
        "wd_status": _pick_column(
            norm, "WD_STATUS", "WORK_DEFINITION_STATUS", "WORK_DEFINITION", "WD", "WD_FLAG"
        ),
        "uom_status": _pick_column(norm, "UOM_STATUS", "UOM", "UOM_CODE", "UOM_FLAG"),
    }


def _parse_report_csv_text(csv_text: str, *, fallback_item: Optional[str] = None) -> Optional[dict]:
    lines = [line for line in (csv_text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        return None
    for row in reader:
        norm = {_norm_col(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        status = _row_to_status(norm, fallback_item=fallback_item)
        if status and (status.get("atp_status") or status.get("wd_status") or status.get("uom_status")):
            return status
    return None


def _parse_report_rows(csv_path: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    if not csv_path.is_file():
        return index

    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return index
        for row in reader:
            norm = {_norm_col(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            status = _row_to_status(norm)
            if status:
                index[status["item_number"]] = status
    return index


def run_report_for_item(item_number: str) -> dict:
    """Fetch ATP/WD/UOM for one item_number via synchronous Fusion runReport."""
    sku = str(item_number or "").strip()
    if not sku:
        raise ValueError("item_number is required")
    if not soap_configured():
        raise FusionSoapConfigError("Set FUSION_SOAP_USER and FUSION_SOAP_PASSWORD in .env")

    now = time.time()
    cached = _item_status_cache.get(sku)
    if cached and (now - cached[0]) < _cache_ttl_seconds():
        return dict(cached[1])

    user = _user()
    password = _password()
    xml = post_fusion_soap(
        _resolve_external_soap_url(),
        build_run_report_envelope(_report_path(), sku, user, password),
        use_body_credentials=True,
        timeout=_run_report_timeout(),
        soap_version="1.2",
    )
    extract_soap_fault(xml)
    report_bytes = extract_xml_element_text(xml, "reportBytes")
    if not report_bytes:
        raise RuntimeError(f"Fusion runReport returned no reportBytes for {sku}")

    csv_text = base64.b64decode(re.sub(r"\s+", "", report_bytes)).decode("utf-8-sig", errors="replace")
    status = _parse_report_csv_text(csv_text, fallback_item=sku)
    if not status:
        raise RuntimeError(f"Fusion runReport returned no ATP/WD/UOM rows for {sku}")

    _item_status_cache[sku] = (now, status)
    return dict(status)


_report_index: Optional[dict[str, dict]] = None


def _load_index(force_reload: bool = False) -> dict[str, dict]:
    global _report_index
    if _report_index is not None and not force_reload:
        return _report_index
    _report_index = _parse_report_rows(CACHE_CSV)
    return _report_index


def cache_status() -> dict:
    fresh = _cache_is_fresh()
    meta: dict = {}
    if CACHE_META.is_file():
        try:
            meta = json.loads(CACHE_META.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "configured": soap_configured(),
        "cache_exists": CACHE_CSV.is_file(),
        "cache_fresh": fresh,
        "cache_path": str(CACHE_CSV),
        "row_count": len(_load_index()) if CACHE_CSV.is_file() else 0,
        "meta": meta,
    }


def lookup_item_atp_wd_uom(item_numbers: list[str], *, allow_refresh: bool = False) -> tuple[dict[str, dict], str]:
    """Lookup ATP/WD/UOM status per item_number. Returns (map, cache_state)."""
    cleaned = sorted({str(i).strip() for i in item_numbers if str(i).strip()})
    if not cleaned:
        return {}, "empty"

    if not soap_configured():
        return {}, "soap_not_configured"

    out: dict[str, dict] = {}
    missing = list(cleaned)

    if CACHE_CSV.is_file():
        index = _load_index(force_reload=not _cache_is_fresh())
        for sku in cleaned:
            if sku in index:
                out[sku] = index[sku]
        missing = [sku for sku in cleaned if sku not in out]

    if missing:
        per_item_errors = 0
        for sku in missing:
            try:
                out[sku] = run_report_for_item(sku)
            except Exception as exc:
                per_item_errors += 1
                logger.warning("Fusion runReport failed for %s: %s", sku, exc)
        if out:
            state = "run_report" if len(out) == len(cleaned) else "partial_run_report"
        elif per_item_errors:
            return {}, "run_report_failed"
        else:
            return {}, "cache_miss"
        return out, state

    if allow_refresh or not _cache_is_fresh():
        sync_on_miss = os.getenv("FUSION_REPORT_SYNC_ON_MISS", "").lower() in {"1", "true", "yes"}
        if allow_refresh or sync_on_miss:
            try:
                fetch_fusion_report_scheduled(force=allow_refresh)
                _load_index(force_reload=True)
            except Exception as exc:
                logger.warning("Fusion report refresh failed: %s", exc)

    if not out:
        return {}, "cache_miss"
    return out, "cache_hit"


def refresh_report_cache() -> dict:
    path = fetch_fusion_report_scheduled(force=True)
    _load_index(force_reload=True)
    return {"path": str(path), "row_count": len(_load_index()), **cache_status()}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Refresh Fusion item ATP/WD/UOM report cache")
    parser.add_argument("--refresh", action="store_true", help="Download report via SOAP")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    args = parser.parse_args()
    if args.refresh:
        print(refresh_report_cache())
    elif args.status:
        print(json.dumps(cache_status(), indent=2))
    else:
        parser.print_help()
