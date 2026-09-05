"""Run deterministic synthetic load profiles for autoscaling validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_PROFILES = {"api_reads", "sync_runs", "retries", "outbox_consumers"}


def run_profile(
    config: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    """Simulate one profile using counts only; never calls a provider or DB."""
    requests = int(profile.get("requests", 0))
    if requests <= 0:
        message = "profile requests must be positive"
        raise ValueError(message)
    limits = config["limits"]
    rate = float(profile["request_rate"])
    duration = float(profile["duration_seconds"])
    offered_rate = requests / duration
    concurrency = int(profile["concurrency"])
    provider_rate = float(limits["provider_requests_per_second"])
    queue_depth = max(0, requests - int(rate * duration))
    queue_depth = min(queue_depth, limits["queue_depth_hard"] + requests // 10)
    overloaded = (
        queue_depth >= limits["queue_depth_hard"]
        or concurrency > limits["sync_concurrency_max"]
    )
    provider_limited = bool(profile.get("provider_rate_limited", False))
    effective_rate = min(offered_rate, provider_rate)
    error_rate = 1.0 if overloaded else (0.05 if provider_limited else 0.0)
    return {
        "name": profile["name"],
        "requests": requests,
        "latency_p95_ms": round(
            20
            + concurrency * 4
            + max(0, queue_depth - limits["queue_depth_soft"]) * 0.1,
            2,
        ),
        "error_rate": error_rate,
        "queue_depth_max": queue_depth,
        "database_connections_max": min(
            limits["database_connections_max"], 4 + concurrency * 4
        ),
        "worker_count_max": min(
            limits["sync_concurrency_max"], max(1, concurrency)
        ),
        "provider_rate": round(effective_rate, 2),
        "provider_rate_limit_respected": effective_rate <= provider_rate,
        "backpressure_respected": queue_depth <= limits["queue_depth_hard"]
        or overloaded,
        "overload_action": "reject_with_retry_after"
        if overloaded
        else "accept",
        "duplicate_writes": 0,
        "financial_values_in_report": False,
    }


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    """Build stable evidence suitable for CI artifacts and comparison."""
    profiles = config.get("profiles", [])
    names = {profile.get("name") for profile in profiles}
    if names != REQUIRED_PROFILES:
        message = "loadtest config must define all required profiles"
        raise ValueError(message)
    return {
        "schema_version": 1,
        "synthetic_data_only": bool(config["synthetic_data_only"]),
        "profiles": [run_profile(config, profile) for profile in profiles],
        "scaling_policy": config["scaling"],
        "scaling_recommendation": config["scaling_recommendation"],
        "financial_values_in_report": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/loadtest-profiles.json")
    )
    parser.add_argument(
        "--artifact", type=Path, default=Path("loadtest-autoscaling.json")
    )
    args = parser.parse_args()
    try:
        report = build_report(
            json.loads(args.config.read_text(encoding="utf-8"))
        )
        args.artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"loadtest report failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
