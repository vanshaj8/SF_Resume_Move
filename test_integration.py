"""Unit and Integration Test Suite for SAP SuccessFactors Resume Snapshot Integration."""

import json
from unittest.mock import MagicMock, patch
import pytest
import requests

from config import SFConfig
from sf_client import (
    SFClient,
    get_candidate_resume,
    get_application_snapshot_status,
    upload_attachment,
    update_application_snapshot,
    orchestrate_resume_snapshot,
    parse_attachment_id,
)


@pytest.fixture
def test_config():
    return SFConfig(
        api_base_url="https://api44preview.sapsf.com",
        default_candidate_id="1104827",
        default_job_req_id="28997",
        default_application_id="547901",
        target_resume_field="Cust_Candidate_Resume",
    )


@pytest.fixture
def mock_client(test_config):
    client = SFClient(config=test_config)
    client.session = MagicMock()
    return client


# ============================================================================
# 1. Attachment ID Parser Tests
# ============================================================================

def test_parse_attachment_id_variations():
    """Test parsing across multiple OData response formats and key structures."""
    # Plain Integer
    assert parse_attachment_id(1234567) == "1234567"
    
    # Plain String
    assert parse_attachment_id("1234567") == "1234567"
    
    # SuccessFactors Key format: Attachment/attachmentId=1234567
    assert parse_attachment_id("Attachment/attachmentId=1234567") == "1234567"
    
    # SuccessFactors OData URI: Attachment(attachmentId=1234567L)
    assert parse_attachment_id("Attachment(attachmentId=1234567L)") == "1234567"
    
    # Simplified URI: Attachment(1234567L)
    assert parse_attachment_id("Attachment(1234567L)") == "1234567"
    
    # Full URL
    assert parse_attachment_id("https://api44preview.sapsf.com/odata/v2/Attachment(1234567L)") == "1234567"
    
    # Standard JSON body format
    assert parse_attachment_id({"d": {"attachmentId": 1234567}}) == "1234567"
    assert parse_attachment_id({"d": {"__metadata": {"uri": "Attachment(987654L)"}}}) == "987654"


def test_parse_attachment_id_invalid():
    """Ensure invalid inputs raise appropriate errors."""
    with pytest.raises(ValueError):
        parse_attachment_id(None)
    with pytest.raises(ValueError):
        parse_attachment_id("no-digits-here")


# ============================================================================
# 2. Candidate Resume Fetch Tests
# ============================================================================

def test_get_candidate_resume_success(mock_client):
    """Test fetching candidate with active resume."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "d": {
            "results": [
                {
                    "candidateId": "1104827",
                    "resume": {
                        "attachmentId": 887766,
                        "fileName": "kavita_resume.pdf",
                        "fileExtension": "pdf",
                        "fileContent": "JVBERi0xLjQKJcfsj6IK...",
                        "mimeType": "application/pdf",
                        "lastModifiedDateTime": "/Date(1708780800000)/",
                    },
                }
            ]
        }
    }
    mock_client.session.request.return_value = mock_resp

    result = get_candidate_resume("1104827", client=mock_client)
    
    assert result is not None
    assert result["candidateId"] == "1104827"
    assert result["fileName"] == "kavita_resume.pdf"
    assert result["fileContent"] == "JVBERi0xLjQKJcfsj6IK..."
    assert result["attachmentId"] == "887766"


def test_get_candidate_resume_no_resume(mock_client):
    """Test candidate exists but has no resume attached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "d": {
            "results": [
                {
                    "candidateId": "1104827",
                    "resume": None,
                }
            ]
        }
    }
    mock_client.session.request.return_value = mock_resp

    result = get_candidate_resume("1104827", client=mock_client)
    assert result is None


def test_get_candidate_resume_empty_file_content(mock_client):
    """Test candidate resume object exists but fileContent is empty string."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "d": {
            "results": [
                {
                    "candidateId": "1104827",
                    "resume": {
                        "fileName": "test.pdf",
                        "fileContent": "",
                    },
                }
            ]
        }
    }
    mock_client.session.request.return_value = mock_resp

    result = get_candidate_resume("1104827", client=mock_client)
    assert result is None


def test_get_candidate_not_found(mock_client):
    """Test candidate record not found."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"d": {"results": []}}
    mock_client.session.request.return_value = mock_resp

    with pytest.raises(ValueError, match="not found"):
        get_candidate_resume("999999", client=mock_client)


# ============================================================================
# 3. Application Snapshot Status & Write-Once Guard Tests
# ============================================================================

def test_get_application_snapshot_status_empty(mock_client):
    """Test JobApplication with empty Cust_Candidate_Resume (ready for write)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "d": {
            "applicationId": "547901",
            "candidateId": "1104827",
            "Cust_Candidate_Resume": None,
        }
    }
    mock_client.session.request.return_value = mock_resp

    status = get_application_snapshot_status("547901", client=mock_client)
    
    assert status["is_populated"] is False
    assert status["current_attachment_id"] is None
    assert status["application_id"] == "547901"


def test_get_application_snapshot_status_already_populated(mock_client):
    """Test JobApplication with Cust_Candidate_Resume already populated (guard active)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "d": {
            "applicationId": "547901",
            "candidateId": "1104827",
            "Cust_Candidate_Resume": {
                "attachmentId": 334455,
                "fileName": "Snapshot_App_547901_resume.pdf",
            },
        }
    }
    mock_client.session.request.return_value = mock_resp

    status = get_application_snapshot_status("547901", client=mock_client)
    
    assert status["is_populated"] is True
    assert status["current_attachment_id"] == "334455"


# ============================================================================
# 4. Attachment Upload Tests
# ============================================================================

def test_upload_attachment_success(mock_client):
    """Test uploading new Attachment entity and parsing response key."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "d": {
            "attachmentId": 776655,
            "fileName": "resume_snapshot.pdf",
            "module": "RECRUITING",
        }
    }
    mock_client.session.request.return_value = mock_resp

    attach_id = upload_attachment(
        base64_content="AQIDBAUGBwgJCgsMDQ4PEA==",
        filename="resume_snapshot.pdf",
        client=mock_client,
    )

    assert attach_id == "776655"
    assert mock_client.session.request.call_count == 1
    call_args = mock_client.session.request.call_args
    assert call_args[1]["json"]["fileName"] == "resume_snapshot.pdf"
    assert call_args[1]["json"]["fileContent"] == "AQIDBAUGBwgJCgsMDQ4PEA=="


def test_upload_attachment_fallback_to_attachment_content(mock_client):
    """Test fallback when tenant schema requires 'attachmentContent' field name."""
    fail_resp = MagicMock()
    fail_resp.status_code = 400
    fail_resp.text = "Property 'fileContent' is invalid. Use 'attachmentContent'."

    success_resp = MagicMock()
    success_resp.status_code = 201
    success_resp.json.return_value = {"d": {"attachmentId": 776655}}

    mock_client.session.request.side_effect = [fail_resp, success_resp]

    attach_id = upload_attachment(
        base64_content="AQIDBAUGBwgJCgsMDQ4PEA==",
        filename="resume_snapshot.pdf",
        client=mock_client,
    )

    assert attach_id == "776655"
    assert mock_client.session.request.call_count == 2
    # Verify second call used attachmentContent
    second_call_args = mock_client.session.request.call_args_list[1]
    assert "attachmentContent" in second_call_args[1]["json"]


# ============================================================================
# 5. Link Attachment to Application Tests
# ============================================================================

def test_update_application_snapshot_patch_success(mock_client):
    """Test updating JobApplication via PATCH."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_client.session.request.return_value = mock_resp

    res = update_application_snapshot("547901", "776655", client=mock_client)
    
    assert res["status"] == "UPDATED"
    assert res["application_id"] == "547901"
    assert res["attachment_id"] == "776655"
    assert mock_client.session.request.call_args[1]["json"] == {
        "Cust_Candidate_Resume": "776655"
    }


# ============================================================================
# 6. Full Orchestration & Business Rules Tests
# ============================================================================

def test_orchestration_write_once_guard_prevents_overwrite(mock_client):
    """Verify write-once rule: if application already has resume snapshot, do nothing."""
    # Step 1: get_application_snapshot_status returns is_populated=True
    app_resp = MagicMock()
    app_resp.status_code = 200
    app_resp.json.return_value = {
        "d": {
            "applicationId": "547901",
            "candidateId": "1104827",
            "Cust_Candidate_Resume": "999888",
        }
    }
    mock_client.session.request.return_value = app_resp

    result = orchestrate_resume_snapshot("1104827", "547901", client=mock_client)

    assert result["status"] == "SKIPPED_ALREADY_EXISTS"
    assert result["attachment_id"] == "999888"
    assert "Write-once guard triggered" in result["message"]
    # Ensure no POST Attachment or PATCH JobApplication was executed
    assert mock_client.session.request.call_count == 1


def test_orchestration_candidate_no_resume_graceful_skip(mock_client):
    """Verify graceful handling when candidate has no resume."""
    # Step 1: JobApplication is empty
    app_resp = MagicMock()
    app_resp.status_code = 200
    app_resp.json.return_value = {
        "d": {
            "applicationId": "547901",
            "Cust_Candidate_Resume": None,
        }
    }
    # Step 2: Candidate has no resume
    cand_resp = MagicMock()
    cand_resp.status_code = 200
    cand_resp.json.return_value = {
        "d": {
            "results": [
                {
                    "candidateId": "1104827",
                    "resume": None,
                }
            ]
        }
    }
    mock_client.session.request.side_effect = [app_resp, cand_resp]

    result = orchestrate_resume_snapshot("1104827", "547901", client=mock_client)

    assert result["status"] == "SKIPPED_NO_RESUME"
    assert "Skipping snapshot creation gracefully" in result["message"]
    assert mock_client.session.request.call_count == 2


def test_orchestration_full_success_flow(mock_client):
    """Verify successful end-to-end resume snapshot creation and linking."""
    # Step 1: JobApplication is empty
    app_resp = MagicMock()
    app_resp.status_code = 200
    app_resp.json.return_value = {
        "d": {
            "applicationId": "547901",
            "Cust_Candidate_Resume": None,
        }
    }
    # Step 2: Candidate has resume
    cand_resp = MagicMock()
    cand_resp.status_code = 200
    cand_resp.json.return_value = {
        "d": {
            "results": [
                {
                    "candidateId": "1104827",
                    "resume": {
                        "fileName": "kavita_resume.pdf",
                        "fileContent": "AQIDBAUGBwgJCgsMDQ4PEA==",
                    },
                }
            ]
        }
    }
    # Step 3: Attachment POST succeeds
    attach_resp = MagicMock()
    attach_resp.status_code = 201
    attach_resp.json.return_value = {
        "d": {
            "attachmentId": 445566,
            "fileName": "Snapshot_App_547901_kavita_resume.pdf",
        }
    }
    # Step 4: JobApplication PATCH succeeds
    patch_resp = MagicMock()
    patch_resp.status_code = 204

    mock_client.session.request.side_effect = [app_resp, cand_resp, attach_resp, patch_resp]

    result = orchestrate_resume_snapshot("1104827", "547901", client=mock_client)

    assert result["status"] == "SUCCESS"
    assert result["attachment_id"] == "445566"
    assert result["application_id"] == "547901"
    assert result["candidate_id"] == "1104827"
    assert mock_client.session.request.call_count == 4
