#!/usr/bin/env python3
"""CLI Runner and Test Case Driver for SAP SuccessFactors Candidate Resume Snapshot Integration.

Test Case Parameters:
  - Candidate ID: 1104827 (kavitap@yopmail.com)
  - Job Requisition ID: 28997
  - Job Application ID: 547901
  - Environment: api44preview.sapsf.com
"""

import argparse
import json
import logging
import sys
from config import SFConfig, default_config
from sf_client import (
    SFClient,
    get_candidate_resume,
    get_application_snapshot_status,
    upload_attachment,
    update_application_snapshot,
    orchestrate_resume_snapshot,
)


def main():
    parser = argparse.ArgumentParser(
        description="SAP SuccessFactors Recruiting - Candidate Resume Snapshot to Job Application"
    )
    parser.add_argument(
        "--candidate-id",
        default=default_config.default_candidate_id,
        help=f"Candidate ID (default: {default_config.default_candidate_id})",
    )
    parser.add_argument(
        "--application-id",
        default=default_config.default_application_id,
        help=f"Job Application ID (default: {default_config.default_application_id})",
    )
    parser.add_argument(
        "--base-url",
        default=default_config.api_base_url,
        help=f"SuccessFactors Base URL (default: {default_config.api_base_url})",
    )
    parser.add_argument(
        "--inspect-metadata",
        action="store_true",
        help="Inspect live $metadata schema for Attachment and JobApplication fields",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger("sf_resume_snapshot").setLevel(logging.DEBUG)

    config = SFConfig(
        api_base_url=args.base_url,
        default_candidate_id=args.candidate_id,
        default_application_id=args.application_id,
    )
    client = SFClient(config=config)

    print("=" * 75)
    print("SAP SuccessFactors Recruiting: Candidate Resume Snapshot to Job Application")
    print("=" * 75)
    print(f"Environment:       {config.api_base_url}")
    print(f"Candidate ID:      {args.candidate_id}")
    print(f"Job Application ID:{args.application_id}")
    print(f"Target Field:      {config.target_resume_field}")
    print("=" * 75)

    if args.inspect_metadata:
        print("\n[Action] Inspecting OData $metadata...")
        meta_result = client.inspect_metadata()
        print(json.dumps(meta_result, indent=2))
        return

    print("\n[Action] Running Guarded Resume Snapshot Orchestration...")
    result = orchestrate_resume_snapshot(
        candidate_id=args.candidate_id,
        app_id=args.application_id,
        client=client,
    )

    print("\n" + "=" * 75)
    print("Execution Result Summary:")
    print("=" * 75)
    print(json.dumps(result, indent=2))

    if result.get("status") == "SUCCESS":
        print("\n✅ Resume snapshot successfully cloned and attached to Job Application.")
        sys.exit(0)
    elif result.get("status") in ("SKIPPED_ALREADY_EXISTS", "SKIPPED_NO_RESUME"):
        print(f"\nℹ️  Process completed safely ({result.get('status')}): {result.get('message')}")
        sys.exit(0)
    else:
        print(f"\n❌ Process failed with error: {result.get('message')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
