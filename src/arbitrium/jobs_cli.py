"""Saved-job runner boundary. Intentionally closed pending deployment approval."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Arbitrium managed job execution boundary")
    parser.add_argument("action", choices=["run"])
    parser.add_argument("--registry", required=True)
    parser.add_argument("--job-id", required=True)
    parser.parse_args(argv)
    # Do not open/create a registry or invoke a saved command before authorization.
    print("Managed job execution is disabled: no action-time approval provider is installed.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
