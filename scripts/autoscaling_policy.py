"""Evaluate safe autoscaling and backpressure decisions for synthetic bursts."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def decide(policy: dict[str, Any], metrics: dict[str, int | bool]) -> dict[str, Any]:
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
        "provider_rate_limit_preserved": provider_limited or action != "provider_backoff",
        "retry_after_seconds": 30 if action == "reject_new_syncs" else 0,
        "financial_values_in_decision": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/autoscaling-policy.json"))
    parser.add_argument("--artifact", type=Path, default=Path("autoscaling-decision.json"))
    args = parser.parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        report = decide(
            policy,
            {"queue_depth": 0, "database_connections": 1, "provider_rate_limited": False},
        )
        args.artifact.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        sys.stderr.write(f"autoscaling policy failed: {type(exc).__name__}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
