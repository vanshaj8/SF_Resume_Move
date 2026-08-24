"""Unit and Integration Test Suite for SAP SuccessFactors Resume Snapshot Integration (Phase 1 & Phase 2)."""

import csv
import os
from unittest.mock import MagicMock, patch
import pytest

from config import SFConfig
from sf_client import (
    SFClient,
    get_candidate_resume,
    get_application_snapshot_status,
    upload_attachment,
    update_application_snapshot,
    orchestrate_resume_snapshot,
    parse_attachment_id,
    discover_applications,
    process_application,
    write_csv_log,
    append_run_summary,
    get_watermark,
    save_watermark,
    run,
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
# Phase 1 Tests: Attachment ID Parsing & Single App Copy
# ============================================================================

def test_parse_attachment_id_variations():
    """Test parsing across multiple OData response formats and key structures."""
    assert parse_attachment_id(1234567) == "1234567"
    assert parse_attachment_id("1234567") == "1234567"
    assert parse_attachment_id("Attachment/attachmentId=1234567") == "1234567"
    assert parse_attachment_id("Attachment(attachmentId=1234567L)") == "1234567"
    assert parse_attachment_id("Attachment(1234567L)") == "1234567"
    assert parse_attachment_id("https://api44preview.sapsf.com/odata/v2/Attachment(1234567L)") == "1234567"
    assert parse_attachment_id({"d": {"attachmentId": 1234567}}) == "1234567"
    assert parse_attachment_id({"d": {"__metadata": {"uri": "Attachment(987654L)"}}}) == "987654"


def test_parse_attachment_id_invalid():
    """Ensure invalid inputs raise appropriate errors."""
    with pytest.raises(ValueError):
        parse_attachment_id(None)
    with pytest.raises(ValueError):
        parse_attachment_id("no-digits-here")


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


def test_get_candidate_resume_no_resume(mock_client):
    """Test candidate exists but has no resume attached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"d": {"results": [{"candidateId": "1104827", "resume": None}]}}
    mock_client.session.request.return_value = mock_resp

    result = get_candidate_resume("1104827", client=mock_client)
    assert result is None


def test_upload_attachment_success(mock_client):
    """Test uploading new Attachment entity."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"d": {"attachmentId": 776655}}
    mock_client.session.request.return_value = mock_resp

    attach_id = upload_attachment(
        base64_content="AQIDBAUGBwgJCgsMDQ4PEA==",
        filename="resume_snapshot.pdf",
        client=mock_client,
    )
    assert attach_id == "776655"


def test_update_application_snapshot_success(mock_client):
    """Test updating JobApplication via PATCH."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_client.session.request.return_value = mock_resp

    res = update_application_snapshot("547901", "776655", client=mock_client)
    assert res["status"] == "UPDATED"
    assert res["application_id"] == "547901"


# ============================================================================
# Phase 2 Tests: Discovery, Batch Processing, CSV Logging & Watermark
# ============================================================================

def test_discover_applications_paginated(mock_client):
    """Test paginated discovery fetching multiple pages until empty result."""
    page1_resp = MagicMock()
    page1_resp.status_code = 200
    page1_resp.json.return_value = {
        "d": {
            "results": [
                {"applicationId": "501", "candidateId": "101", "Cust_Candidate_Resume": None},
                {"applicationId": "502", "candidateId": "102", "Cust_Candidate_Resume": "112233"},
            ]
        }
    }

    page2_resp = MagicMock()
    page2_resp.status_code = 200
    page2_resp.json.return_value = {"d": {"results": []}}

    # Discovery with page_size=2 should request page 1 then page 2
    mock_client.session.request.side_effect = [page1_resp, page2_resp]

    apps = discover_applications("2026-08-25T00:00:00Z", client=mock_client, page_size=2)
    assert len(apps) == 2
    assert apps[0]["applicationId"] == "501"
    assert apps[1]["applicationId"] == "502"
    assert mock_client.session.request.call_count == 2


def test_process_application_skipped_already_set(mock_client):
    """Test write-once guard in batch: app with existing attachment is SKIPPED_ALREADY_SET."""
    app = {
        "applicationId": "502",
        "candidateId": "102",
        "Cust_Candidate_Resume": "112233",
    }
    
    row = process_application(app, run_timestamp="2026-08-25T14:00:00Z", client=mock_client)
    
    assert row["status"] == "SKIPPED_ALREADY_SET"
    assert row["applicationId"] == "502"
    assert row["candidateId"] == "102"
    assert row["attachmentId"] == ""
    assert row["errorMessage"] == ""
    # Ensure no API calls were made
    assert mock_client.session.request.call_count == 0


def test_process_application_skipped_no_resume(mock_client):
    """Test app with empty custom resume when candidate has no profile resume."""
    app = {
        "applicationId": "503",
        "candidateId": "103",
        "Cust_Candidate_Resume": None,
    }
    # Mock candidate query returning no resume
    cand_resp = MagicMock()
    cand_resp.status_code = 200
    cand_resp.json.return_value = {"d": {"results": [{"candidateId": "103", "resume": None}]}}
    mock_client.session.request.return_value = cand_resp

    row = process_application(app, run_timestamp="2026-08-25T14:00:00Z", client=mock_client)

    assert row["status"] == "SKIPPED_NO_RESUME"
    assert row["applicationId"] == "503"
    assert row["attachmentId"] == ""
    assert row["errorMessage"] == ""


def test_process_application_success(mock_client):
    """Test successful snapshot upload and linking for empty application."""
    app = {
        "applicationId": "504",
        "candidateId": "104",
        "Cust_Candidate_Resume": None,
    }
    # 1. Candidate query
    cand_resp = MagicMock()
    cand_resp.status_code = 200
    cand_resp.json.return_value = {
        "d": {
            "results": [{
                "candidateId": "104",
                "resume": {
                    "fileName": "cand104_resume.pdf",
                    "fileContent": "AQIDBAUGBwgJCgsMDQ4PEA==",
                }
            }]
        }
    }
    # 2. Attachment POST
    attach_resp = MagicMock()
    attach_resp.status_code = 201
    attach_resp.json.return_value = {"d": {"attachmentId": 998877}}
    # 3. JobApplication PATCH
    patch_resp = MagicMock()
    patch_resp.status_code = 204

    mock_client.session.request.side_effect = [cand_resp, attach_resp, patch_resp]

    row = process_application(app, run_timestamp="2026-08-25T14:00:00Z", client=mock_client)

    assert row["status"] == "SUCCESS"
    assert row["applicationId"] == "504"
    assert row["candidateId"] == "104"
    assert row["attachmentId"] == "998877"
    assert row["errorMessage"] == ""


def test_process_application_failed_error_isolation(mock_client):
    """Test that failure during upload sets FAILED without crashing the batch."""
    app = {
        "applicationId": "505",
        "candidateId": "105",
        "Cust_Candidate_Resume": None,
    }
    # 1. Candidate query succeeds
    cand_resp = MagicMock()
    cand_resp.status_code = 200
    cand_resp.json.return_value = {
        "d": {
            "results": [{
                "candidateId": "105",
                "resume": {
                    "fileName": "resume.pdf",
                    "fileContent": "AQIDBA==",
                }
            }]
        }
    }
    # 2. Attachment POST fails
    attach_resp = MagicMock()
    attach_resp.status_code = 500
    attach_resp.text = "Internal Server Error in Attachment Service"

    mock_client.session.request.side_effect = [cand_resp, attach_resp]

    row = process_application(app, run_timestamp="2026-08-25T14:00:00Z", client=mock_client)

    assert row["status"] == "FAILED"
    assert row["applicationId"] == "505"
    assert row["attachmentId"] == ""
    assert "Attachment creation failed" in row["errorMessage"]


def test_write_csv_log(tmp_path):
    """Test generation of per-run flat CSV log."""
    log_dir = str(tmp_path / "logs")
    rows = [
        {
            "runTimestamp": "2026-08-25T14:00:00Z",
            "applicationId": "501",
            "candidateId": "101",
            "status": "SUCCESS",
            "attachmentId": "998877",
            "errorMessage": "",
        },
        {
            "runTimestamp": "2026-08-25T14:00:00Z",
            "applicationId": "502",
            "candidateId": "102",
            "status": "SKIPPED_ALREADY_SET",
            "attachmentId": "",
            "errorMessage": "",
        },
    ]

    filepath = write_csv_log(rows, run_timestamp="2026-08-25T14:00:00Z", log_dir=log_dir)
    assert os.path.exists(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        read_rows = list(reader)
        assert len(read_rows) == 2
        assert read_rows[0]["applicationId"] == "501"
        assert read_rows[0]["status"] == "SUCCESS"
        assert read_rows[0]["attachmentId"] == "998877"
        assert read_rows[1]["status"] == "SKIPPED_ALREADY_SET"


def test_append_run_summary(tmp_path):
    """Test appending to cumulative summary CSV across multiple runs."""
    summary_path = str(tmp_path / "test_summary.csv")
    
    run1 = {
        "runTimestamp": "2026-08-25T13:00:00Z",
        "applicationsFound": 10,
        "succeeded": 5,
        "skippedAlreadySet": 3,
        "skippedNoResume": 2,
        "failed": 0,
        "runStatus": "COMPLETED",
    }
    run2 = {
        "runTimestamp": "2026-08-25T14:00:00Z",
        "applicationsFound": 4,
        "succeeded": 2,
        "skippedAlreadySet": 1,
        "skippedNoResume": 0,
        "failed": 1,
        "runStatus": "COMPLETED",
    }

    append_run_summary(run1, summary_filepath=summary_path)
    append_run_summary(run2, summary_filepath=summary_path)

    with open(summary_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        entries = list(reader)
        assert len(entries) == 2
        assert entries[0]["runTimestamp"] == "2026-08-25T13:00:00Z"
        assert entries[0]["succeeded"] == "5"
        assert entries[1]["runTimestamp"] == "2026-08-25T14:00:00Z"
        assert entries[1]["failed"] == "1"


def test_watermark_lifecycle(tmp_path):
    """Test watermark read, save, and default fallback."""
    wm_file = str(tmp_path / "watermark.txt")
    
    # Default when file absent
    assert get_watermark(watermark_file=wm_file) == "1970-01-01T00:00:00Z"

    # Save new watermark
    save_watermark("2026-08-25T14:00:00Z", watermark_file=wm_file)
    assert get_watermark(watermark_file=wm_file) == "2026-08-25T14:00:00Z"


def test_run_batch_advances_watermark_on_completed(mock_client, tmp_path):
    """Test full batch run advances watermark when status is COMPLETED."""
    wm_file = str(tmp_path / "watermark.txt")
    log_dir = str(tmp_path / "logs")
    summary_file = str(tmp_path / "summary.csv")

    save_watermark("2026-08-25T12:00:00Z", watermark_file=wm_file)

    # 1. Discovery returns 1 app
    disc_resp = MagicMock()
    disc_resp.status_code = 200
    disc_resp.json.return_value = {
        "d": {
            "results": [
                {"applicationId": "901", "candidateId": "801", "Cust_Candidate_Resume": "EXISTS"}
            ]
        }
    }
    mock_client.session.request.return_value = disc_resp

    summary = run(
        client=mock_client,
        watermark_file=wm_file,
        log_dir=log_dir,
        summary_file=summary_file,
    )

    assert summary["runStatus"] == "COMPLETED"
    assert summary["applicationsFound"] == 1
    assert summary["skippedAlreadySet"] == 1
    assert summary["watermarkAdvanced"] is True

    # Check that watermark was advanced past 2026-08-25T12:00:00Z
    new_wm = get_watermark(watermark_file=wm_file)
    assert new_wm != "2026-08-25T12:00:00Z"
    assert os.path.exists(summary["csvLogPath"])
    assert os.path.exists(summary_file)


def test_run_batch_preserves_watermark_on_errored(mock_client, tmp_path):
    """Test full batch run preserves existing watermark if discovery errors out."""
    wm_file = str(tmp_path / "watermark.txt")
    log_dir = str(tmp_path / "logs")
    summary_file = str(tmp_path / "summary.csv")

    initial_wm = "2026-08-25T12:00:00Z"
    save_watermark(initial_wm, watermark_file=wm_file)

    # Discovery fails with HTTP 500
    disc_resp = MagicMock()
    disc_resp.status_code = 500
    disc_resp.text = "Database Connection Timeout"
    mock_client.session.request.return_value = disc_resp

    summary = run(
        client=mock_client,
        watermark_file=wm_file,
        log_dir=log_dir,
        summary_file=summary_file,
    )

    assert summary["runStatus"] == "ERRORED"
    assert summary["watermarkAdvanced"] is False
    # Ensure watermark was NOT overwritten
    assert get_watermark(watermark_file=wm_file) == initial_wm
