# SAP SuccessFactors Recruiting — Candidate Resume Snapshot to Job Application Integration

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![OData v2](https://img.shields.io/badge/OData-v2.0-orange.svg)](https://www.odata.org/documentation/odata-version-2-0/)
[![Tests](https://img.shields.io/badge/tests-14%20passed-brightgreen.svg)]()

## Overview & Architecture

In SAP SuccessFactors Recruiting:
- The `Candidate` profile's `resume` field is a **floating pointer** (representing the candidate's latest, globally uploaded resume).
- Each `JobApplication`'s custom attachment field (`Cust_Candidate_Resume`) must represent a **frozen, write-once audit snapshot** taken at the time that specific application was first processed.

This integration executes the guarded snapshot copy logic adhering strictly to the non-negotiable write-once rule.

### Process Flow

```mermaid
flowchart TD
    Start([Process Application]) --> Step1[1. Check JobApplication.Cust_Candidate_Resume status]
    Step1 --> CheckPopulated{Already populated?}
    CheckPopulated -- Yes --> SkipGuard[SKIP: Guard Enforced - Snapshot already frozen]
    CheckPopulated -- No --> Step2[2. GET Candidate profile resume with $expand=resume]
    Step2 --> CheckResume{Candidate has resume?}
    CheckResume -- No --> SkipNoResume[SKIP: Candidate has no resume - Exit gracefully]
    CheckResume -- Yes --> Step3[3. POST /odata/v2/Attachment with Base64 content]
    Step3 --> Step4[4. Parse new Attachment ID]
    Step4 --> Step5[5. PATCH /odata/v2/JobApplication Cust_Candidate_Resume]
    Step5 --> Done([SUCCESS: Snapshot attached and immutable])
```

---

## Key Modules & Function Signatures

All core functions are implemented in [`sf_client.py`](file:///Users/vanshajsharma/MoveResume/sf_client.py):

| Function | Purpose |
|---|---|
| `get_candidate_resume(candidate_id, client=None)` | Queries Candidate entity with `$expand=resume` and extracts Base64 `fileContent`. Returns `None` gracefully if resume is empty/absent. |
| `get_application_snapshot_status(app_id, client=None)` | Evaluates `JobApplication.Cust_Candidate_Resume` to enforce the write-once guard. |
| `upload_attachment(base64_content, filename, module="RECRUITING", client=None)` | Creates a new independent `Attachment` object in SuccessFactors and returns the clean Attachment ID. |
| `update_application_snapshot(app_id, attachment_id, client=None)` | Links the new attachment to `JobApplication.Cust_Candidate_Resume` via `PATCH` (with `MERGE`/`upsert` fallback). |
| `orchestrate_resume_snapshot(candidate_id, app_id, client=None)` | Orchestrates the entire guarded copy lifecycle for a single application. |

---

## Test Case Reference

| Attribute | Value |
|---|---|
| **Candidate ID** | `1104827` (`kavitap@yopmail.com`) |
| **Job Requisition ID** | `28997` |
| **Job Application ID** | `547901` |
| **Environment** | `https://api44preview.sapsf.com` |
| **Source Field** | `Candidate.resume` |
| **Target Field** | `JobApplication.Cust_Candidate_Resume` |

---

## Running the Integration & Tests

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment (Optional for Live Execution)
Copy `.env.example` to `.env` and configure your API credentials:
```bash
cp .env.example .env
```

### 3. Run Automated Unit & Integration Tests
```bash
pytest test_integration.py -v
```

### 4. Execute CLI Runner
```bash
# Execute test case against instance
python main.py --candidate-id 1104827 --application-id 547901

# Inspect live tenant $metadata for field validation
python main.py --inspect-metadata
```
