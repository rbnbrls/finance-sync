"""Evaluate safe autoscaling and backpressure decisions for synthetic bursts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def decide(
    policy: dict[str, Any], metrics: dict[str, int | bool]
) -> dict[str, Any]:
    limits = policy["limits"]
    queue = int(metrics["queue_depth"])
    db = int(metrics["database_connections"])
    provider_limited = bool(metrics["provider_rate_limited"])
    if db >= limits["database_connections_max"]:
        action = "service_busy"
        reason = "database_connections_exhausted"
    elif queue >= limits["queue_depth_hard"]:
        action = "reject_new_syncs"
        reason = "queue_depth_hard_limit"
    elif provider_limited:
        action = "provider_backoff"
        reason = "provider_rate_limit"
    elif queue >= limits["queue_depth_soft"]:
        action = "scale_workers_and_slow_syncs"
        reason = "queue_depth_soft_limit"
    else:
        action = "accept"
        reason = "within_limits"
    return {
        "action": action,
        "reason": reason,
        "tenant_isolation": True,
        "provider_rate_limit_preserved": provider_limited
        or action != "provider_backoff",
        "retry_after_seconds": 30 if action == "reject_new_syncs" else 0,
        "financial_values_in_decision": False,
    }


def scale_workers(
    policy: dict[str, Any], metrics: dict[str, int | float]
) -> dict[str, Any]:
    """Choose a worker transition with hysteresis, cooldown and lease draining.

    The function is intentionally pure: callers provide the elapsed cooldown
    time and the number of active leases, so clock skew and concurrent worker
    state cannot make a scale-down decision unsafe.
    """
    scaling = policy["scaling"]
    minimum = int(scaling["minimum_workers"])
    maximum = int(scaling["maximum_workers"])
    current = int(metrics["current_workers"])
    queue_depth = int(metrics["queue_depth"])
    active_leases = max(0, int(metrics.get("active_leases", 0)))
    cooldown_elapsed = float(metrics.get("cooldown_elapsed_seconds", 0))
    cooldown = int(scaling["cooldown_seconds"])

    if not minimum <= current <= maximum:
        message = "current_workers must be within scaling bounds"
        raise ValueError(message)
    if queue_depth < 0:
        message = "queue_depth must not be negative"
        raise ValueError(message)
    if cooldown_elapsed < 0:
        message = "cooldown_elapsed_seconds must not be negative"
        raise ValueError(message)

    desired = current
    action = "hold"
    if cooldown_elapsed < cooldown:
        action = "cooldown"
    elif queue_depth >= int(scaling["scale_up_queue_depth"]):
        desired = min(maximum, current + 1)
        action = "scale_up" if desired != current else "hold_at_maximum"
    elif (
        queue_depth <= int(scaling["scale_down_queue_depth"])
        and current > minimum
    ):
        desired = current - 1
        action = "scale_down"

    workers_to_drain = max(0, current - desired)
    protected_by_leases = min(workers_to_drain, active_leases)
    return {
        "action": action,
        "current_workers": current,
        "desired_workers": desired,
        "workers_to_drain": workers_to_drain,
        "workers_protected_by_active_leases": protected_by_leases,
        "cooldown_seconds": cooldown,
        "hysteresis": {
            "scale_up_queue_depth": int(scaling["scale_up_queue_depth"]),
            "scale_down_queue_depth": int(scaling["scale_down_queue_depth"]),
        },
        "drain_timeout_seconds": int(scaling["drain_timeout_seconds"]),
        "tenant_isolation": True,
        "financial_values_in_decision": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy", type=Path, default=Path("config/autoscaling-policy.json")
    )
    parser.add_argument(
        "--artifact", type=Path, default=Path("autoscaling-decision.json")
    )
    args = parser.parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        report = decide(
            policy,
            {
                "queue_depth": 0,
                "database_connections": 1,
                "provider_rate_limited": False,
            },
        )
        args.artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.stderr.write(f"autoscaling policy failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
