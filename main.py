#!/usr/bin/env python3
"""CLI Runner and Batch Driver for SAP SuccessFactors Candidate Resume Snapshot Integration (Phase 1 & Phase 2)."""

import argparse
import json
import logging
import sys
from config import SFConfig, default_config
from sf_client import (
    SFClient,
    orchestrate_resume_snapshot,
    run,
)


def main():
    parser = argparse.ArgumentParser(
        description="SAP SuccessFactors Recruiting - Candidate Resume Snapshot Integration (Batch & Single App)"
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run Phase 2 scheduled batch process with watermark and CSV logging (Default)",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help="Run single-application mode (Phase 1 test case)",
    )
    parser.add_argument(
        "--candidate-id",
        default=default_config.default_candidate_id,
        help=f"Candidate ID for single app mode (default: {default_config.default_candidate_id})",
    )
    parser.add_argument(
        "--application-id",
        default=default_config.default_application_id,
        help=f"Job Application ID for single app mode (default: {default_config.default_application_id})",
    )
    parser.add_argument(
        "--base-url",
        default=default_config.api_base_url,
        help=f"SuccessFactors Base URL (default: {default_config.api_base_url})",
    )
    parser.add_argument(
        "--watermark-file",
        default="watermark.txt",
        help="Path to watermark file storing lastRunTimestamp (default: watermark.txt)",
    )
    parser.add_argument(
        "--since",
        help="Manually override lastRunTimestamp for discovery (e.g. 2026-08-25T00:00:00Z)",
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

    print("=" * 78)
    print("SAP SuccessFactors Recruiting: Candidate Resume Snapshot Integration")
    print("=" * 78)
    print(f"Environment:       {config.api_base_url}")
    print(f"Target Field:      {config.target_resume_field}")

    if args.inspect_metadata:
        print("\n[Action] Inspecting OData $metadata...")
        meta_result = client.inspect_metadata()
        print(json.dumps(meta_result, indent=2))
        return

    # Single application mode
    if args.single and not args.batch:
        print(f"Mode:              Single Application Test")
        print(f"Candidate ID:      {args.candidate_id}")
        print(f"Job Application ID:{args.application_id}")
        print("=" * 78)

        print("\n[Action] Running Guarded Resume Snapshot for single application...")
        result = orchestrate_resume_snapshot(
            candidate_id=args.candidate_id,
            app_id=args.application_id,
            client=client,
        )

        print("\n" + "=" * 78)
        print("Execution Result Summary:")
        print("=" * 78)
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

    # Batch mode (Default)
    print(f"Mode:              Scheduled Batch Discovery & Processing")
    print(f"Watermark File:    {args.watermark_file}")
    if args.since:
        print(f"Lookback Override: {args.since}")
    print("=" * 78)

    print("\n[Action] Executing Batch Engine...")
    summary = run(
        client=client,
        watermark_file=args.watermark_file,
        lookback_override=args.since,
    )

    print("\n" + "=" * 78)
    print("Batch Run Summary:")
    print("=" * 78)
    print(json.dumps(summary, indent=2))

    if summary.get("runStatus") == "COMPLETED":
        print(f"\n✅ Batch run completed. Log written to: {summary.get('csvLogPath')}")
        sys.exit(0)
    else:
        print(f"\n❌ Batch run errored. Summary logged to: {summary.get('summaryFilePath')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
