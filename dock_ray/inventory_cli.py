"""Cron entry point. Secrets come only from the process environment."""
import asyncio
import os
import sys

from .inventory import InventoryReporter


def main():
    args = sys.argv[1:]
    if any(arg not in ("--force", "--help") for arg in args):
        print("Usage: dockray-inventory [--force]", file=sys.stderr)
        return 1
    if "--help" in args:
        print("Usage: dockray-inventory [--force]. Set RAY_INVENTORY=true, RAY_ENVIRONMENT and project credentials.")
        return 0
    reporter = InventoryReporter(
        enabled=os.getenv("RAY_INVENTORY") == "true", token=os.getenv("RAY_TOKEN", ""),
        private_key=os.getenv("RAY_PRIVATE_KEY", ""), environment=os.getenv("RAY_ENVIRONMENT", "production"),
        project_directory=os.getenv("RAY_INVENTORY_PROJECT_DIR"), state_directory=os.getenv("RAY_INVENTORY_STATE_DIR"),
        requirements_file=os.getenv("RAY_INVENTORY_REQUIREMENTS_FILE"),
        exclude_packages=tuple(x.strip() for x in os.getenv("RAY_INVENTORY_EXCLUDE_PACKAGES", "").split(",") if x.strip()),
        optional_groups=tuple(x.strip() for x in os.getenv("RAY_INVENTORY_OPTIONAL_GROUPS", "").split(",") if x.strip()),
    )
    result = asyncio.run(reporter.report(force="--force" in args))
    print("DockRay inventory: " + result["status"])
    return 0 if result["status"] in ("sent", "disabled", "not_due", "backoff", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
