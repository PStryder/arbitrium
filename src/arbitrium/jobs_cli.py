"""Saved-job runner. Unconfigured, absent, expired or consumed grants deny execution."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Arbitrium managed job execution boundary")
    parser.add_argument("action", choices=["run"])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--grant-id")
    args = parser.parse_args(argv)
    # There is intentionally no --approve, grant-issuance or module-loading option.
    if not args.grant_id:
        print("Managed execution denied: an exact operational grant is required.", file=sys.stderr)
        return 3
    try:
        from .approvals import ApprovalStore
        from .catalog import Catalog
        from .jobs import Registry, Runner, uuid_id
        uuid_id(args.job_id)
        uuid_id(args.grant_id)
        root = Path(args.registry).resolve()
        if not (root / "jobs.sqlite3").is_file() or not (root / "catalog.json").is_file():
            raise PermissionError("Managed runtime is not configured")
        catalog = Catalog.load(root / "catalog.json")
        registry = Registry(root)
        catalog.validate_job(registry.job(args.job_id))
        approvals = ApprovalStore(registry, catalog, initialize=False)
        approvals.describe(args.grant_id)
        # Expired unstarted attempts can recover; uncertain started attempts keep
        # their hold. This never signals or relaunches a persisted process ID.
        registry.recover()
        worker = Runner(registry, catalog.templates,
                        authorize=lambda job, run_id: approvals.authorize(args.grant_id, job, run_id),
                        resolve_environment=catalog.resolve_environment)
        result = worker.execute(args.job_id)
        print(json.dumps({key: result[key] for key in ("job_id", "run_id", "status", "exit_code", "reason_code")}))
        return 0 if result["status"] == "completed" else 3 if result["reason_code"] == "approval_denied" else 1
    except Exception:
        # Exceptions can originate in environment resolution or local paths.
        # Never print secrets, argument vectors, file contents or raw exceptions.
        print("Managed execution denied or unavailable; inspect the local lifecycle registry.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
