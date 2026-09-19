"""Secret-safety and low-cardinality contracts for destination metrics."""

from finance_sync.observability.destination_metrics import (
    DESTINATION_PROBE_PARITY,
    record_destination_probe,
)


def test_destination_metrics_accept_only_safe_aggregate_counts() -> None:
    status = "metrics-contract"
    record_destination_probe(
        destination="wealthfolio",
        status=status,
        duration_seconds=0.25,
        parity={
            "remote_accounts": 2,
            "remote_assets": 3,
            "secret": 999,
            "raw_account_id": 123,
        },
    )

    assert (
        DESTINATION_PROBE_PARITY.labels(
            "wealthfolio", status, "remote_accounts"
        )._value.get()
        == 2
    )
    assert (
        DESTINATION_PROBE_PARITY.labels(
            "wealthfolio", status, "remote_assets"
        )._value.get()
        == 3
    )
    assert "secret" not in DESTINATION_PROBE_PARITY._metrics
