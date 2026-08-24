"""SAP SuccessFactors Recruiting OData v2 Integration Client.

Provides reusable functions to copy a candidate's resume snapshot
from the Candidate profile to a Job Application with write-once guard.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple
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
            "User-Agent": "SF-Resume-Snapshot-Integration/1.0",
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
# Reusable Integration Functions
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
      
    Raises:
      RuntimeError: If candidate is not found or API returns an error status.
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
        # Check single entity format d (if direct key was returned)
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

    # In SF OData, resume might be wrapped in {'results': [...]} if multi-valued, or direct dict
    if isinstance(resume_obj, dict) and "results" in resume_obj:
        res_list = resume_obj.get("results", [])
        resume_obj = res_list[0] if res_list else None

    if not resume_obj or not isinstance(resume_obj, dict):
        logger.info("Candidate %s has empty resume record.", cand_id_str)
        return None

    # Check for binary content
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
    """Check whether JobApplication.Cust_Candidate_Resume is already populated.
    
    This acts as the write-once guard check before creating or linking any attachments.
    
    Returns:
      Dict with keys:
        - "application_id": str
        - "candidate_id": Optional[str]
        - "is_populated": bool (True if Cust_Candidate_Resume is already set)
        - "current_attachment_id": Optional[str] (Parsed attachment ID if already set)
        - "raw_value": Any
    """
    sf = client or SFClient()
    application_id_str = str(app_id).strip()
    field_name = target_field or sf.config.target_resume_field

    logger.info(
        "Checking snapshot status for JobApplication %s (Target field: %s)",
        application_id_str,
        field_name,
    )

    # We can query JobApplication('{app_id}') or filter by applicationId
    endpoint = f"JobApplication('{application_id_str}')"
    params = {
        "$select": f"applicationId,candidateId,{field_name}",
        "$expand": field_name,
    }

    resp = sf.request("GET", endpoint, params=params)

    # Fallback to $filter query if single key syntax returns 404 or not found
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

    is_populated = False
    current_attachment_id = None

    if raw_custom_resume is not None:
        # Check if it's an expanded Attachment navigation object
        if isinstance(raw_custom_resume, dict):
            if "__deferred" in raw_custom_resume:
                # If deferred and not null, we check if direct property had an ID or query attachment
                pass
            elif raw_custom_resume.get("attachmentId"):
                is_populated = True
                current_attachment_id = str(raw_custom_resume.get("attachmentId"))
            elif raw_custom_resume.get("fileContent") or raw_custom_resume.get("fileName"):
                is_populated = True
                current_attachment_id = str(raw_custom_resume.get("attachmentId", "EXISTS"))
        elif isinstance(raw_custom_resume, (int, str)) and str(raw_custom_resume).strip() not in ("", "0", "null", "None"):
            is_populated = True
            try:
                current_attachment_id = parse_attachment_id(raw_custom_resume)
            except Exception:
                current_attachment_id = str(raw_custom_resume)

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
    """Create a new independent Attachment entity in SAP SuccessFactors.
    
    POST /odata/v2/Attachment
    Payload: { "fileName": filename, "fileContent": base64_content, "module": "RECRUITING", "viewable": true }
    
    Returns:
      Plain string Attachment ID (e.g. "123456")
    """
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

    # Extract Attachment ID from response body or headers
    resp_data = {}
    try:
        resp_data = resp.json()
    except Exception:
        pass

    attachment_id = None

    # Check JSON response body
    if resp_data:
        d_obj = resp_data.get("d", resp_data)
        if isinstance(d_obj, dict):
            if "attachmentId" in d_obj and d_obj["attachmentId"]:
                attachment_id = str(d_obj["attachmentId"])
            elif "__metadata" in d_obj and "uri" in d_obj["__metadata"]:
                attachment_id = parse_attachment_id(d_obj["__metadata"]["uri"])
            elif "key" in d_obj:
                attachment_id = parse_attachment_id(d_obj["key"])

    # Check Location header or other headers if body did not contain ID
    if not attachment_id:
        location_header = resp.headers.get("Location") or resp.headers.get("location")
        if location_header:
            attachment_id = parse_attachment_id(location_header)

    # Fallback to parsing raw response text
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
    """Link the new attachment to the Job Application's custom resume field.
    
    Executes PATCH /odata/v2/JobApplication('{app_id}') with fallback to MERGE / upsert.
    
    Payload:
      {
        "Cust_Candidate_Resume": "<attachmentId>"
      }
    """
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
    
    # Try PATCH first
    resp = sf.request("PATCH", endpoint, json_data=payload)

    # If PATCH method is not supported (e.g. 405 Method Not Allowed), try MERGE or POST upsert
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
    """Orchestrate the guarded resume snapshot copy for a single candidate/application pair.
    
    Enforces business rules:
      1. Guard: Check if JobApplication.Cust_Candidate_Resume is already populated.
         If yes -> Skip without modifying (Write-Once Idempotent guarantee).
      2. Source: Read candidate's current resume from Candidate Profile.
         If candidate has no resume -> Skip gracefully.
      3. Attachment: Create a new independent Attachment entity with the candidate's resume content.
      4. Target Link: Link the new Attachment ID to the Job Application's Cust_Candidate_Resume.
      
    Returns:
      Dict with status code, message, and metadata:
        - "status": "SUCCESS" | "SKIPPED_ALREADY_EXISTS" | "SKIPPED_NO_RESUME" | "ERROR"
        - "message": Human-readable description
        - "application_id": str
        - "candidate_id": str
        - "attachment_id": Optional[str]
    """
    sf = client or SFClient()
    candidate_id_str = str(candidate_id).strip()
    app_id_str = str(app_id).strip()

    logger.info(
        "=== Starting Resume Snapshot Copy: Candidate ID=%s -> Application ID=%s ===",
        candidate_id_str,
        app_id_str,
    )

    try:
        # Step 1: Guard Check on JobApplication
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

        # Step 2: Read Candidate's Current Resume
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

        # Step 3: Create Independent Attachment Object
        # Format snapshot filename cleanly to indicate application context
        orig_filename = candidate_resume.get("fileName", "resume.pdf")
        snapshot_filename = f"Snapshot_App_{app_id_str}_{orig_filename}"
        
        new_attachment_id = upload_attachment(
            base64_content=candidate_resume["fileContent"],
            filename=snapshot_filename,
            module="RECRUITING",
            client=sf,
        )

        # Step 4: Link New Attachment to Job Application
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
