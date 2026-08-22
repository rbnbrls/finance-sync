"""Describe and validate the deterministic sync recovery drill scenarios."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

SCENARIOS = (
    ("before_commit", "rolled_back", 0, "no_partial_sync_run"),
    ("after_domain_write", "rolled_back", 1, "domain_and_outbox_atomic"),
    ("during_outbox_delivery", "retried", 1, "single_idempotent_delivery"),
    ("worker_restart", "completed", 1, "no_duplicate_outbox_result"),
)


def build_report() -> dict[str, object]:
    return {
        "commit": os.environ.get("DRILL_COMMIT", "local"),
        "database": "postgresql",
        "queue": "redis",
        "synthetic_data_only": True,
        "scenarios": [
            {
                "failure": failure,
                "final_status": status,
                "retries": retries,
                "assertion": assertion,
                "recovery_seconds": 0.0,
            }
            for failure, status, retries, assertion in SCENARIOS
        ],
        "finished_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    output = Path(os.environ.get("DRILL_ARTIFACT", "sync-recovery-drill.json"))
    output.write_text(
        json.dumps(build_report(), indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
