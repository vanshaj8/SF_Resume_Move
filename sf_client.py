"""SAP SuccessFactors Recruiting OData v2 Integration Client (Phase 1 & Phase 2).

Provides reusable functions to copy a candidate's resume snapshot
from the Candidate profile to Job Applications with write-once guard,
supporting single-application copy as well as scheduled batch processing
with watermark management and CSV auditing.
"""

import csv
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import SFConfig, default_config

# Set up module logger
logger = logging.getLogger("sf_resume_snapshot")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class SFClient:
    """HTTP Client for SAP SuccessFactors OData v2 API interactions."""

    def __init__(self, config: Optional[SFConfig] = None):
        self.config = config or default_config
        self.session = self._create_session()
        self.base_odata_url = self.config.get_base_odata_url()
        self._cached_attachment_content_field: Optional[str] = None
        self._cached_target_field_name: Optional[str] = None

    def _create_session(self) -> requests.Session:
        """Create a configured requests.Session with connection pooling and retries."""
        session = requests.Session()
        
        # Configure retry strategy for transient network/server errors
        retries = Retry(
            total=self.config.max_retries,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PATCH", "MERGE", "PUT"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # Configure Default Headers
        session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SF-Resume-Snapshot-Integration/2.0",
        })

        # Set Authentication
        if self.config.bearer_token:
            session.headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        else:
            auth_tuple = self.config.get_basic_auth_tuple()
            if auth_tuple:
                session.auth = auth_tuple

        return session

    def request(
        self,
        method: str,
        path_or_url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_data: Optional[Any] = None,
    ) -> requests.Response:
        """Execute an HTTP request against the SuccessFactors API."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            clean_path = path_or_url.lstrip("/")
            url = f"{self.base_odata_url}/{clean_path}"

        req_headers = headers or {}
        
        logger.debug("Executing %s %s with params=%s", method, url, params)
        response = self.session.request(
            method=method,
            url=url,
            headers=req_headers,
            params=params,
            data=data,
            json=json_data,
            timeout=self.config.timeout_seconds,
        )
        return response

    def inspect_metadata(self) -> Dict[str, Any]:
        """Fetch and analyze $metadata schema to discover live field names and entity structures."""
        url = f"{self.base_odata_url}/$metadata"
        logger.info("Fetching OData metadata from %s", url)
        try:
            resp = self.request("GET", url, headers={"Accept": "application/xml"})
            if resp.status_code == 200:
                content = resp.text
                
                # Check Attachment content field name in live metadata
                has_attachment_content = "Name=\"attachmentContent\"" in content or "Name=\"attachmentcontent\"" in content
                has_file_content = "Name=\"fileContent\"" in content or "Name=\"filecontent\"" in content
                
                field_name = "fileContent"
                if has_attachment_content and not has_file_content:
                    field_name = "attachmentContent"
                self._cached_attachment_content_field = field_name
                
                # Check Custom Resume field name in JobApplication
                target_field = self.config.target_resume_field
                target_found = target_field in content
                
                logger.info(
                    "Metadata analysis: Attachment content field='%s', Target field '%s' present=%s",
                    field_name,
                    target_field,
                    target_found,
                )
                return {
                    "attachment_content_field": field_name,
                    "target_field_present": target_found,
                    "raw_metadata_length": len(content),
                }
            else:
                logger.warning("Metadata request returned status %d. Using standard defaults.", resp.status_code)
                return {"attachment_content_field": "fileContent", "target_field_present": True}
        except Exception as ex:
            logger.warning("Could not fetch $metadata (%s). Falling back to standard schema.", ex)
            return {"attachment_content_field": "fileContent", "target_field_present": True}

    def get_attachment_content_field_name(self) -> str:
        """Return the appropriate field name for binary content in Attachment entity."""
        if self._cached_attachment_content_field:
            return self._cached_attachment_content_field
        # Default is standard OData v2 'fileContent'
        return "fileContent"


# ============================================================================
# Phase 1: Core Atomic Functions
# ============================================================================

def parse_attachment_id(raw_response_or_key: Any) -> str:
    """Parse and extract the clean, plain Attachment ID from various SF OData key/response formats.
    
    Handles formats such as:
      - Raw integer: 123456
      - Plain string: '123456'
      - JSON dict: {'d': {'attachmentId': 123456}} or {'attachmentId': '123456'}
      - Key URI: 'Attachment/attachmentId=123456'
      - Key URI: 'Attachment(attachmentId=123456L)'
      - Key URI: 'Attachment(123456L)'
      - URL: 'https://.../odata/v2/Attachment(123456L)'
    """
    if raw_response_or_key is None:
        raise ValueError("Cannot parse attachment ID from None")

    if isinstance(raw_response_or_key, int):
        return str(raw_response_or_key)

    if isinstance(raw_response_or_key, dict):
        d_obj = raw_response_or_key.get("d", raw_response_or_key)
        if isinstance(d_obj, dict):
            # Check standard fields in json response
            if "attachmentId" in d_obj and d_obj["attachmentId"] is not None:
                return str(d_obj["attachmentId"])
            if "id" in d_obj and d_obj["id"] is not None:
                return str(d_obj["id"])
            if "__metadata" in d_obj and "uri" in d_obj["__metadata"]:
                raw_response_or_key = d_obj["__metadata"]["uri"]

    str_val = str(raw_response_or_key).strip()

    # If it's already purely numeric
    if str_val.isdigit():
        return str_val

    # Regex patterns for SuccessFactors OData Attachment keys
    patterns = [
        r"attachmentId\s*=\s*['\"]?(\d+)",          # attachmentId=12345 or Attachment/attachmentId=12345
        r"Attachment\s*\(\s*attachmentId\s*=\s*(\d+)",  # Attachment(attachmentId=12345L)
        r"Attachment\s*\(\s*(\d+)",                  # Attachment(12345L)
        r"Attachment/(\d+)",                         # Attachment/12345
        r"/Attachment\('?(\d+)'?\)",                 # /Attachment('12345')
    ]

    for pat in patterns:
        match = re.search(pat, str_val, re.IGNORECASE)
        if match:
            return match.group(1)

    # Fallback: extract any contiguous digits if present
    digit_match = re.search(r"(\d+)", str_val)
    if digit_match:
        return digit_match.group(1)

    raise ValueError(f"Could not parse a valid Attachment ID from: '{raw_response_or_key}'")


def is_attachment_populated(raw_val: Any) -> Tuple[bool, Optional[str]]:
    """Determine if a custom attachment field value is populated and return parsed ID if available."""
    if raw_val is None:
        return False, None
    
    if isinstance(raw_val, dict):
        if "__deferred" in raw_val:
            return False, None
        if raw_val.get("attachmentId"):
            return True, str(raw_val.get("attachmentId"))
        if raw_val.get("fileContent") or raw_val.get("fileName"):
            return True, str(raw_val.get("attachmentId", "EXISTS"))
        return False, None

    if isinstance(raw_val, (int, str)):
        s = str(raw_val).strip()
        if s and s not in ("0", "null", "None", "false", "False"):
            try:
                aid = parse_attachment_id(s)
                return True, aid
            except Exception:
                return True, s

    return False, None


def get_candidate_resume(
    candidate_id: str,
    client: Optional[SFClient] = None,
) -> Optional[Dict[str, Any]]:
    """Read the candidate's current resume from the Candidate profile via OData $expand.
    
    OData Request:
      GET /odata/v2/Candidate?$select=candidateId,resume/attachmentId,resume/fileContent,resume/fileExtension,resume/fileName,resume/lastModifiedDateTime,resume/mimeType&$expand=resume&$filter=(candidateId eq '{candidateId}')
      
    Returns:
      Dict with resume properties if present and has fileContent:
        {
          "fileName": str,
          "fileExtension": str,
          "fileContent": str (Base64),
          "mimeType": str,
          "attachmentId": str,
          "lastModifiedDateTime": str,
          "candidateId": str
        }
      Or None if candidate has no resume or fileContent is null/empty.
    """
    sf = client or SFClient()
    cand_id_str = str(candidate_id).strip()
    
    logger.info("Fetching Candidate profile and resume for candidateId=%s", cand_id_str)

    endpoint = "Candidate"
    params = {
        "$select": "candidateId,resume/attachmentId,resume/fileContent,resume/fileExtension,resume/fileName,resume/lastModifiedDateTime,resume/mimeType",
        "$expand": "resume",
        "$filter": f"(candidateId eq {cand_id_str})" if cand_id_str.isdigit() else f"(candidateId eq '{cand_id_str}')",
    }

    resp = sf.request("GET", endpoint, params=params)

    if resp.status_code != 200:
        logger.error("Failed to query Candidate %s: %d %s", cand_id_str, resp.status_code, resp.text)
        raise RuntimeError(f"OData Candidate query failed with status {resp.status_code}: {resp.text}")

    data = resp.json()
    results = data.get("d", {}).get("results", [])
    if not results:
        d_obj = data.get("d", {})
        if d_obj.get("candidateId") == cand_id_str or str(d_obj.get("candidateId")) == cand_id_str:
            results = [d_obj]

    if not results:
        raise ValueError(f"Candidate with candidateId={cand_id_str} not found in SuccessFactors.")

    candidate_record = results[0]
    resume_obj = candidate_record.get("resume")

    if not resume_obj:
        logger.info("Candidate %s exists but has no resume navigation object.", cand_id_str)
        return None

    if isinstance(resume_obj, dict) and "results" in resume_obj:
        res_list = resume_obj.get("results", [])
        resume_obj = res_list[0] if res_list else None

    if not resume_obj or not isinstance(resume_obj, dict):
        logger.info("Candidate %s has empty resume record.", cand_id_str)
        return None

    file_content = resume_obj.get("fileContent") or resume_obj.get("attachmentContent")
    if not file_content:
        logger.info("Candidate %s has resume metadata but empty fileContent.", cand_id_str)
        return None

    filename = resume_obj.get("fileName") or f"resume_{cand_id_str}.pdf"
    file_ext = resume_obj.get("fileExtension") or ""
    mime_type = resume_obj.get("mimeType") or "application/pdf"
    attachment_id = str(resume_obj.get("attachmentId") or "")
    last_mod = resume_obj.get("lastModifiedDateTime") or ""

    logger.info(
        "Found Candidate resume: fileName='%s', fileExtension='%s', size=%d base64 chars, attachmentId=%s",
        filename,
        file_ext,
        len(file_content),
        attachment_id,
    )

    return {
        "candidateId": cand_id_str,
        "fileName": filename,
        "fileExtension": file_ext,
        "fileContent": file_content,
        "mimeType": mime_type,
        "attachmentId": attachment_id,
        "lastModifiedDateTime": last_mod,
    }


def get_application_snapshot_status(
    app_id: str,
    client: Optional[SFClient] = None,
    target_field: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether JobApplication.Cust_Candidate_Resume is already populated."""
    sf = client or SFClient()
    application_id_str = str(app_id).strip()
    field_name = target_field or sf.config.target_resume_field

    logger.info("Checking snapshot status for JobApplication %s (Target field: %s)", application_id_str, field_name)

    endpoint = f"JobApplication('{application_id_str}')"
    params = {
        "$select": f"applicationId,candidateId,{field_name}",
        "$expand": field_name,
    }

    resp = sf.request("GET", endpoint, params=params)

    if resp.status_code == 404:
        logger.debug("Direct key access returned 404. Trying filter query for JobApplication %s", application_id_str)
        endpoint = "JobApplication"
        params = {
            "$select": f"applicationId,candidateId,{field_name}",
            "$expand": field_name,
            "$filter": f"(applicationId eq {application_id_str})" if application_id_str.isdigit() else f"(applicationId eq '{application_id_str}')",
        }
        resp = sf.request("GET", endpoint, params=params)

    if resp.status_code != 200:
        logger.error("Failed to query JobApplication %s: %d %s", application_id_str, resp.status_code, resp.text)
        raise RuntimeError(f"OData JobApplication query failed with status {resp.status_code}: {resp.text}")

    data = resp.json()
    d_obj = data.get("d", {})
    if "results" in d_obj:
        results = d_obj.get("results", [])
        if not results:
            raise ValueError(f"JobApplication with applicationId={application_id_str} not found.")
        app_record = results[0]
    else:
        app_record = d_obj

    candidate_id = str(app_record.get("candidateId") or "")
    raw_custom_resume = app_record.get(field_name)

    is_populated, current_attachment_id = is_attachment_populated(raw_custom_resume)

    logger.info(
        "Application %s Snapshot Status: is_populated=%s, current_attachment_id=%s",
        application_id_str,
        is_populated,
        current_attachment_id,
    )

    return {
        "application_id": application_id_str,
        "candidate_id": candidate_id,
        "is_populated": is_populated,
        "current_attachment_id": current_attachment_id,
        "raw_value": raw_custom_resume,
    }


def upload_attachment(
    base64_content: str,
    filename: str,
    module: str = "RECRUITING",
    client: Optional[SFClient] = None,
) -> str:
    """Create a new independent Attachment entity in SAP SuccessFactors."""
    sf = client or SFClient()
    if not base64_content:
        raise ValueError("base64_content must not be empty.")
    if not filename:
        filename = "candidate_resume_snapshot.pdf"

    content_field = sf.get_attachment_content_field_name()
    
    payload = {
        "fileName": filename,
        content_field: base64_content,
        "module": module,
        "viewable": True,
    }

    logger.info("Uploading new Attachment (fileName='%s', module='%s', content_field='%s')", filename, module, content_field)
    
    endpoint = "Attachment"
    resp = sf.request("POST", endpoint, json_data=payload)

    # In case the tenant strictly requires 'attachmentContent' instead of 'fileContent' or vice-versa
    if resp.status_code in (400, 500) and content_field == "fileContent":
        error_text = resp.text.lower()
        if "attachmentcontent" in error_text or "invalid property" in error_text:
            logger.info("Retrying Attachment upload using 'attachmentContent' field...")
            payload["attachmentContent"] = base64_content
            del payload["fileContent"]
            resp = sf.request("POST", endpoint, json_data=payload)

    if resp.status_code not in (200, 201):
        logger.error("Failed to create Attachment: %d %s", resp.status_code, resp.text)
        raise RuntimeError(f"OData Attachment creation failed with status {resp.status_code}: {resp.text}")

    resp_data = {}
    try:
        resp_data = resp.json()
    except Exception:
        pass

    attachment_id = None

    if resp_data:
        d_obj = resp_data.get("d", resp_data)
        if isinstance(d_obj, dict):
            if "attachmentId" in d_obj and d_obj["attachmentId"]:
                attachment_id = str(d_obj["attachmentId"])
            elif "__metadata" in d_obj and "uri" in d_obj["__metadata"]:
                attachment_id = parse_attachment_id(d_obj["__metadata"]["uri"])
            elif "key" in d_obj:
                attachment_id = parse_attachment_id(d_obj["key"])

    if not attachment_id:
        location_header = resp.headers.get("Location") or resp.headers.get("location")
        if location_header:
            attachment_id = parse_attachment_id(location_header)

    if not attachment_id and resp.text:
        attachment_id = parse_attachment_id(resp.text)

    if not attachment_id:
        raise ValueError(f"Could not extract Attachment ID from response: {resp.text}")

    logger.info("Successfully created Attachment with plain ID: %s", attachment_id)
    return str(attachment_id)


def update_application_snapshot(
    app_id: str,
    attachment_id: str,
    client: Optional[SFClient] = None,
    target_field: Optional[str] = None,
) -> Dict[str, Any]:
    """Link the new attachment to the Job Application's custom resume field."""
    sf = client or SFClient()
    application_id_str = str(app_id).strip()
    attach_id_str = str(attachment_id).strip()
    field_name = target_field or sf.config.target_resume_field

    logger.info(
        "Linking Attachment %s to JobApplication %s (%s)",
        attach_id_str,
        application_id_str,
        field_name,
    )

    payload = {
        field_name: attach_id_str,
    }

    endpoint = f"JobApplication('{application_id_str}')"
    resp = sf.request("PATCH", endpoint, json_data=payload)

    if resp.status_code == 405:
        logger.info("PATCH returned 405. Trying MERGE on %s", endpoint)
        resp = sf.request("MERGE", endpoint, json_data=payload)

    if resp.status_code in (404, 405, 501):
        logger.info("Trying OData upsert POST on JobApplication...")
        upsert_payload = {
            "__metadata": {"uri": "JobApplication"},
            "applicationId": application_id_str,
            field_name: attach_id_str,
        }
        resp = sf.request("POST", "upsert", json_data=upsert_payload)

    if resp.status_code not in (200, 204):
        logger.error("Failed to update JobApplication %s: %d %s", application_id_str, resp.status_code, resp.text)
        raise RuntimeError(f"OData JobApplication update failed with status {resp.status_code}: {resp.text}")

    logger.info("Successfully linked Attachment %s to JobApplication %s", attach_id_str, application_id_str)
    return {
        "status": "UPDATED",
        "application_id": application_id_str,
        "attachment_id": attach_id_str,
        "target_field": field_name,
        "http_status": resp.status_code,
    }


def orchestrate_resume_snapshot(
    candidate_id: str,
    app_id: str,
    client: Optional[SFClient] = None,
) -> Dict[str, Any]:
    """Orchestrate the guarded resume snapshot copy for a single candidate/application pair (Phase 1)."""
    sf = client or SFClient()
    candidate_id_str = str(candidate_id).strip()
    app_id_str = str(app_id).strip()

    logger.info(
        "=== Starting Resume Snapshot Copy: Candidate ID=%s -> Application ID=%s ===",
        candidate_id_str,
        app_id_str,
    )

    try:
        app_status = get_application_snapshot_status(app_id_str, client=sf)
        
        if app_status.get("is_populated"):
            existing_attach_id = app_status.get("current_attachment_id")
            msg = (
                f"Write-once guard triggered: JobApplication {app_id_str} already has a frozen snapshot "
                f"linked (Attachment ID: {existing_attach_id}). Preserving existing snapshot without overwrite."
            )
            logger.info(msg)
            return {
                "status": "SKIPPED_ALREADY_EXISTS",
                "message": msg,
                "application_id": app_id_str,
                "candidate_id": candidate_id_str,
                "attachment_id": existing_attach_id,
            }

        candidate_resume = get_candidate_resume(candidate_id_str, client=sf)
        
        if not candidate_resume or not candidate_resume.get("fileContent"):
            msg = (
                f"Candidate {candidate_id_str} has no resume on their Candidate Profile (fileContent is null/empty). "
                f"Skipping snapshot creation gracefully."
            )
            logger.info(msg)
            return {
                "status": "SKIPPED_NO_RESUME",
                "message": msg,
                "application_id": app_id_str,
                "candidate_id": candidate_id_str,
                "attachment_id": None,
            }

        orig_filename = candidate_resume.get("fileName", "resume.pdf")
        snapshot_filename = f"Snapshot_App_{app_id_str}_{orig_filename}"
        
        new_attachment_id = upload_attachment(
            base64_content=candidate_resume["fileContent"],
            filename=snapshot_filename,
            module="RECRUITING",
            client=sf,
        )

        update_application_snapshot(
            app_id=app_id_str,
            attachment_id=new_attachment_id,
            client=sf,
        )

        msg = (
            f"Successfully captured resume snapshot from Candidate {candidate_id_str} "
            f"and linked Attachment {new_attachment_id} to JobApplication {app_id_str}."
        )
        logger.info(msg)
        return {
            "status": "SUCCESS",
            "message": msg,
            "application_id": app_id_str,
            "candidate_id": candidate_id_str,
            "attachment_id": new_attachment_id,
            "filename": snapshot_filename,
        }

    except Exception as err:
        error_msg = f"Error during resume snapshot orchestration: {str(err)}"
        logger.exception(error_msg)
        return {
            "status": "ERROR",
            "message": error_msg,
            "application_id": app_id_str,
            "candidate_id": candidate_id_str,
            "attachment_id": None,
        }


# ============================================================================
# Phase 2: Batch Discovery, Watermark, Processing & CSV Logging
# ============================================================================

def format_odata_timestamp_filter(timestamp_str: str) -> str:
    """Format an ISO timestamp into an OData v2 compatible filter expression."""
    clean_ts = timestamp_str.strip()
    if clean_ts.startswith("datetimeoffset'") or clean_ts.startswith("datetime'"):
        return f"applicationDate gt {clean_ts}"
    # Standard format: applicationDate gt datetimeoffset'YYYY-MM-DDTHH:MM:SSZ' or datetime'...'
    if "T" in clean_ts:
        return f"applicationDate gt datetimeoffset'{clean_ts}'"
    return f"applicationDate gt '{clean_ts}'"


def discover_applications(
    last_run_timestamp: str,
    client: Optional[SFClient] = None,
    page_size: int = 1000,
    target_field: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Discover Job Applications created/modified since lastRunTimestamp with pagination.
    
    OData Query:
      GET /odata/v2/JobApplication
          ?$filter=applicationDate gt '{lastRunTimestamp}'
          &$select=applicationId,candidateId,Cust_Candidate_Resume
          &$top=1000&$skip={n}
          
    Paginates until an empty result set is returned.
    """
    sf = client or SFClient()
    field_name = target_field or sf.config.target_resume_field
    all_applications: List[Dict[str, Any]] = []
    skip = 0

    filter_expr = format_odata_timestamp_filter(last_run_timestamp)
    logger.info("Starting discovery for JobApplications with filter: %s (page_size=%d)", filter_expr, page_size)

    while True:
        params = {
            "$filter": filter_expr,
            "$select": f"applicationId,candidateId,{field_name}",
            "$top": page_size,
            "$skip": skip,
        }

        resp = sf.request("GET", "JobApplication", params=params)

        if resp.status_code != 200:
            # If datetimeoffset filter syntax caused error, fallback to simple quoted or datetime filter
            if resp.status_code in (400, 500) and "datetimeoffset" in filter_expr:
                logger.info("Retrying discovery using standard datetime'...' syntax...")
                clean_iso = last_run_timestamp.replace("Z", "")
                filter_expr = f"applicationDate gt datetime'{clean_iso}'"
                params["$filter"] = filter_expr
                resp = sf.request("GET", "JobApplication", params=params)

            if resp.status_code != 200:
                logger.error("JobApplication discovery failed at skip=%d: %d %s", skip, resp.status_code, resp.text)
                raise RuntimeError(f"OData discovery query failed with status {resp.status_code}: {resp.text}")

        data = resp.json()
        results = data.get("d", {}).get("results", [])
        
        if not results:
            logger.info("Discovery reached end of stream at skip=%d. Total applications found: %d", skip, len(all_applications))
            break

        all_applications.extend(results)
        logger.info("Discovered %d applications in current page (Total so far: %d)", len(results), len(all_applications))

        if len(results) < page_size:
            # Reached last page
            break

        skip += page_size

    return all_applications


def process_application(
    app: Dict[str, Any],
    run_timestamp: Optional[str] = None,
    client: Optional[SFClient] = None,
    target_field: Optional[str] = None,
) -> Dict[str, str]:
    """Process a single Job Application record per the write-once guard rule.
    
    Logic:
      if Cust_Candidate_Resume is not empty:
          status = SKIPPED_ALREADY_SET
      else:
          resume = get_candidate_resume(candidateId)
          if resume is None:
              status = SKIPPED_NO_RESUME
          else:
              try:
                  attachment_id = upload_attachment(resume.fileContent, resume.fileName)
                  update_application_snapshot(applicationId, attachment_id)
                  status = SUCCESS
              except Exception as e:
                  status = FAILED, errorMessage = str(e)
                  
    Returns:
      A CSV log row dictionary:
        {
          "runTimestamp": str,
          "applicationId": str,
          "candidateId": str,
          "status": "SUCCESS" | "SKIPPED_ALREADY_SET" | "SKIPPED_NO_RESUME" | "FAILED",
          "attachmentId": str,
          "errorMessage": str
        }
    """
    sf = client or SFClient()
    current_run_ts = run_timestamp or datetime.now(timezone.utc).isoformat()
    field_name = target_field or sf.config.target_resume_field

    app_id = str(app.get("applicationId") or "").strip()
    cand_id = str(app.get("candidateId") or "").strip()
    raw_custom_resume = app.get(field_name)

    is_populated, existing_id = is_attachment_populated(raw_custom_resume)

    # 1. Guard Check: Is snapshot already set?
    if is_populated:
        logger.info("JobApplication %s already has Cust_Candidate_Resume set (Attachment ID: %s). Skipping.", app_id, existing_id)
        return {
            "runTimestamp": current_run_ts,
            "applicationId": app_id,
            "candidateId": cand_id,
            "status": "SKIPPED_ALREADY_SET",
            "attachmentId": "",
            "errorMessage": "",
        }

    # 2. Check Candidate Resume
    try:
        candidate_resume = get_candidate_resume(cand_id, client=sf)
    except Exception as ex:
        logger.warning("Error fetching candidate resume for %s: %s", cand_id, ex)
        return {
            "runTimestamp": current_run_ts,
            "applicationId": app_id,
            "candidateId": cand_id,
            "status": "FAILED",
            "attachmentId": "",
            "errorMessage": f"Failed fetching candidate resume: {str(ex)}",
        }

    if candidate_resume is None or not candidate_resume.get("fileContent"):
        logger.info("Candidate %s has no resume. Skipping JobApplication %s.", cand_id, app_id)
        return {
            "runTimestamp": current_run_ts,
            "applicationId": app_id,
            "candidateId": cand_id,
            "status": "SKIPPED_NO_RESUME",
            "attachmentId": "",
            "errorMessage": "",
        }

    # 3. Create independent attachment and link to JobApplication
    try:
        orig_filename = candidate_resume.get("fileName", "resume.pdf")
        snapshot_filename = f"Snapshot_App_{app_id}_{orig_filename}"

        new_attach_id = upload_attachment(
            base64_content=candidate_resume["fileContent"],
            filename=snapshot_filename,
            module="RECRUITING",
            client=sf,
        )

        update_application_snapshot(
            app_id=app_id,
            attachment_id=new_attach_id,
            client=sf,
            target_field=field_name,
        )

        logger.info("Successfully created and linked snapshot attachment %s to JobApplication %s", new_attach_id, app_id)
        return {
            "runTimestamp": current_run_ts,
            "applicationId": app_id,
            "candidateId": cand_id,
            "status": "SUCCESS",
            "attachmentId": new_attach_id,
            "errorMessage": "",
        }

    except Exception as ex:
        err_msg = str(ex)
        logger.error("Failed snapshot copy for application %s: %s", app_id, err_msg)
        return {
            "runTimestamp": current_run_ts,
            "applicationId": app_id,
            "candidateId": cand_id,
            "status": "FAILED",
            "attachmentId": "",
            "errorMessage": err_msg,
        }


def write_csv_log(
    rows: List[Dict[str, str]],
    run_timestamp: str,
    log_dir: str = "logs",
) -> str:
    """Write per-run flat CSV log file.
    
    Naming: resume_snapshot_log_{safe_run_timestamp}.csv
    Columns: runTimestamp, applicationId, candidateId, status, attachmentId, errorMessage
    """
    os.makedirs(log_dir, exist_ok=True)
    safe_ts = run_timestamp.replace(":", "-").replace(".", "-").replace("+", "_")
    filename = f"resume_snapshot_log_{safe_ts}.csv"
    filepath = os.path.join(log_dir, filename)

    fieldnames = ["runTimestamp", "applicationId", "candidateId", "status", "attachmentId", "errorMessage"]

    logger.info("Writing %d application log rows to CSV: %s", len(rows), filepath)
    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "runTimestamp": row.get("runTimestamp", run_timestamp),
                "applicationId": row.get("applicationId", ""),
                "candidateId": row.get("candidateId", ""),
                "status": row.get("status", ""),
                "attachmentId": row.get("attachmentId", ""),
                "errorMessage": row.get("errorMessage", ""),
            })

    return filepath


def append_run_summary(
    summary_row: Dict[str, Any],
    summary_filepath: str = "resume_snapshot_run_summary.csv",
) -> str:
    """Append a one-line run summary to the cumulative summary CSV file.
    
    Columns: runTimestamp, applicationsFound, succeeded, skippedAlreadySet, skippedNoResume, failed, runStatus
    """
    file_exists = os.path.exists(summary_filepath)
    fieldnames = [
        "runTimestamp",
        "applicationsFound",
        "succeeded",
        "skippedAlreadySet",
        "skippedNoResume",
        "failed",
        "runStatus",
    ]

    logger.info("Appending run summary to cumulative file: %s", summary_filepath)
    with open(summary_filepath, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists or os.path.getsize(summary_filepath) == 0:
            writer.writeheader()
        
        writer.writerow({
            "runTimestamp": summary_row.get("runTimestamp", ""),
            "applicationsFound": summary_row.get("applicationsFound", 0),
            "succeeded": summary_row.get("succeeded", 0),
            "skippedAlreadySet": summary_row.get("skippedAlreadySet", 0),
            "skippedNoResume": summary_row.get("skippedNoResume", 0),
            "failed": summary_row.get("failed", 0),
            "runStatus": summary_row.get("runStatus", "COMPLETED"),
        })

    return summary_filepath


def get_watermark(
    watermark_file: str = "watermark.txt",
    default_timestamp: str = "1970-01-01T00:00:00Z",
) -> str:
    """Read the last successful run's start timestamp from the watermark file."""
    if os.path.exists(watermark_file):
        try:
            with open(watermark_file, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    logger.info("Read existing watermark timestamp: %s", val)
                    return val
        except Exception as ex:
            logger.warning("Could not read watermark file %s (%s). Using default.", watermark_file, ex)
    
    logger.info("No existing watermark found. Using initial watermark: %s", default_timestamp)
    return default_timestamp


def save_watermark(
    timestamp_str: str,
    watermark_file: str = "watermark.txt",
) -> None:
    """Save the new watermark timestamp after a successful batch run."""
    logger.info("Advancing watermark to: %s in %s", timestamp_str, watermark_file)
    with open(watermark_file, "w", encoding="utf-8") as f:
        f.write(timestamp_str.strip() + "\n")


def run(
    client: Optional[SFClient] = None,
    watermark_file: str = "watermark.txt",
    log_dir: str = "logs",
    summary_file: str = "resume_snapshot_run_summary.csv",
    lookback_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the Phase 2 scheduled batch process with watermark management and CSV logging.
    
    Flow:
      1. Capture runTimestamp (run start time in UTC).
      2. Read lastRunTimestamp from watermark file.
      3. Discover Job Applications created since lastRunTimestamp (paginated).
      4. Process each application with the write-once guard.
      5. Generate per-run CSV log and append cumulative run summary.
      6. Advance watermark ONLY if runStatus is COMPLETED (never on ERRORED).
    """
    sf = client or SFClient()
    
    # 1. Capture run start time
    run_start_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("=== Starting Batch Integration Run at %s ===", run_start_timestamp)

    # 2. Read lastRunTimestamp
    last_run_timestamp = lookback_override or get_watermark(watermark_file=watermark_file)
    logger.info("Processing window: applicationDate > %s", last_run_timestamp)

    rows: List[Dict[str, str]] = []
    succeeded = 0
    skipped_already_set = 0
    skipped_no_resume = 0
    failed = 0
    run_status = "COMPLETED"

    try:
        # 3. Discovery Query (Paginated)
        apps = discover_applications(last_run_timestamp=last_run_timestamp, client=sf)
        applications_found = len(apps)
        logger.info("Discovered %d applications to process.", applications_found)

        # 4. Per-Application Processing Loop
        for app in apps:
            row = process_application(app=app, run_timestamp=run_start_timestamp, client=sf)
            rows.append(row)
            
            st = row.get("status")
            if st == "SUCCESS":
                succeeded += 1
            elif st == "SKIPPED_ALREADY_SET":
                skipped_already_set += 1
            elif st == "SKIPPED_NO_RESUME":
                skipped_no_resume += 1
            elif st == "FAILED":
                failed += 1

    except Exception as batch_err:
        logger.exception("Fatal batch error during application discovery/processing: %s", batch_err)
        run_status = "ERRORED"
        applications_found = len(rows)

    # 5. Write per-run flat CSV log
    csv_log_path = write_csv_log(rows=rows, run_timestamp=run_start_timestamp, log_dir=log_dir)

    # 6. Append to cumulative summary CSV
    summary_data = {
        "runTimestamp": run_start_timestamp,
        "applicationsFound": applications_found,
        "succeeded": succeeded,
        "skippedAlreadySet": skipped_already_set,
        "skippedNoResume": skipped_no_resume,
        "failed": failed,
        "runStatus": run_status,
    }
    append_run_summary(summary_row=summary_data, summary_filepath=summary_file)

    # 7. Advance watermark ONLY on COMPLETED
    if run_status == "COMPLETED":
        save_watermark(timestamp_str=run_start_timestamp, watermark_file=watermark_file)
        logger.info("Batch run COMPLETED successfully. Watermark advanced to %s", run_start_timestamp)
    else:
        logger.warning("Batch run ended in ERRORED state. Watermark preserved at %s for safe retry.", last_run_timestamp)

    summary_data["csvLogPath"] = csv_log_path
    summary_data["summaryFilePath"] = summary_file
    summary_data["watermarkAdvanced"] = (run_status == "COMPLETED")

    logger.info("=== Batch Run Finished: Status=%s (Found: %d, Succeeded: %d, SkippedSet: %d, SkippedNoResume: %d, Failed: %d) ===",
                run_status, applications_found, succeeded, skipped_already_set, skipped_no_resume, failed)
    return summary_data
