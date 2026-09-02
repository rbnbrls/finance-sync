"""Release 18 autoscaling and backpressure contracts."""

# pyright: basic

import json
from pathlib import Path

from scripts.autoscaling_policy import decide, scale_workers


def test_burst_scenarios_apply_safe_backpressure() -> None:
    policy = json.loads(Path("config/autoscaling-policy.json").read_text())
    assert (
        decide(
            policy,
            {
                "queue_depth": 10,
                "database_connections": 10,
                "provider_rate_limited": False,
            },
        )["action"]
        == "accept"
    )
    assert (
        decide(
            policy,
            {
                "queue_depth": 100,
                "database_connections": 10,
                "provider_rate_limited": False,
            },
        )["action"]
        == "scale_workers_and_slow_syncs"
    )
    hard = decide(
        policy,
        {
            "queue_depth": 600,
            "database_connections": 10,
            "provider_rate_limited": False,
        },
    )
    assert hard["action"] == "reject_new_syncs"
    assert hard["retry_after_seconds"] > 0
    db = decide(
        policy,
        {
            "queue_depth": 10,
            "database_connections": 40,
            "provider_rate_limited": False,
        },
    )
    assert db["action"] == "service_busy"


def test_provider_rate_limit_and_tenant_isolation_are_preserved() -> None:
    policy = json.loads(Path("config/autoscaling-policy.json").read_text())
    limited = decide(
        policy,
        {
            "queue_depth": 10,
            "database_connections": 10,
            "provider_rate_limited": True,
        },
    )
    assert limited["action"] == "provider_backoff"
    assert limited["tenant_isolation"] is True
    assert limited["provider_rate_limit_preserved"] is True
    assert limited["financial_values_in_decision"] is False


def test_ci_validates_autoscaling_policy() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "autoscaling-policy:" in workflow
    assert "autoscaling_policy.py" in workflow


def test_scaling_hysteresis_prevents_thrashing() -> None:
    policy = json.loads(Path("config/autoscaling-policy.json").read_text())
    low = scale_workers(
        policy,
        {
            "current_workers": 2,
            "queue_depth": 49,
            "cooldown_elapsed_seconds": 60,
        },
    )
    assert low["action"] == "hold"
    high = scale_workers(
        policy,
        {
            "current_workers": 2,
            "queue_depth": 50,
            "cooldown_elapsed_seconds": 60,
        },
    )
    assert high["action"] == "scale_up"
    assert high["desired_workers"] == 3


def test_scaling_cooldown_keeps_workers_stable() -> None:
    policy = json.loads(Path("config/autoscaling-policy.json").read_text())
    result = scale_workers(
        policy,
        {
            "current_workers": 3,
            "queue_depth": 100,
            "cooldown_elapsed_seconds": 59,
        },
    )
    assert result["action"] == "cooldown"
    assert result["desired_workers"] == 3


def test_scaling_drains_workers_without_interrupting_active_leases() -> None:
    policy = json.loads(Path("config/autoscaling-policy.json").read_text())
    result = scale_workers(
        policy,
        {
            "current_workers": 4,
            "queue_depth": 0,
            "active_leases": 2,
            "cooldown_elapsed_seconds": 60,
        },
    )
    assert result["action"] == "scale_down"
    assert result["desired_workers"] == 3
    assert result["workers_protected_by_active_leases"] == 1
    assert result["drain_timeout_seconds"] == 300
