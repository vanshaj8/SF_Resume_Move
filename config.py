"""Configuration module for SAP SuccessFactors Recruiting Resume Snapshot Integration."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Load optional .env file if present
load_dotenv()


@dataclass
class SFConfig:
    """SuccessFactors API connection and integration configuration."""

    # API Endpoint configuration
    api_base_url: str = os.getenv("SF_API_BASE_URL", "https://api44preview.sapsf.com")
    
    # Credentials & Company info
    company_id: str = os.getenv("SF_COMPANY_ID", "")
    username: str = os.getenv("SF_USERNAME", "")
    password: str = os.getenv("SF_PASSWORD", "")
    bearer_token: str = os.getenv("SF_BEARER_TOKEN", "")

    # Target & Source Field definitions
    source_resume_field: str = os.getenv("SF_SOURCE_RESUME_FIELD", "resume")
    target_resume_field: str = os.getenv("SF_TARGET_RESUME_FIELD", "Cust_Candidate_Resume")
    
    # Default test case settings
    default_candidate_id: str = os.getenv("SF_TEST_CANDIDATE_ID", "1104827")
    default_job_req_id: str = os.getenv("SF_TEST_JOB_REQ_ID", "28997")
    default_application_id: str = os.getenv("SF_TEST_APP_ID", "547901")

    # Network / HTTP settings
    timeout_seconds: int = int(os.getenv("SF_HTTP_TIMEOUT", "30"))
    max_retries: int = int(os.getenv("SF_MAX_RETRIES", "3"))

    def get_base_odata_url(self) -> str:
        """Return the base OData v2 URL, normalizing trailing slashes."""
        base = self.api_base_url.rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        return f"{base}/odata/v2"

    def get_basic_auth_tuple(self) -> tuple[str, str] | None:
        """Return (username@company_id, password) for Basic Auth or None if not configured."""
        if not self.username or not self.password:
            return None
        user = f"{self.username}@{self.company_id}" if self.company_id else self.username
        return (user, self.password)


# Singleton default config instance
default_config = SFConfig()
