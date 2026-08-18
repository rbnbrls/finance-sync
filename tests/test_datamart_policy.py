"""Unit tests for governed datamart policy evaluation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from finance_sync.api.v1.datamarts import DataMartCreate, GrantCreate
from finance_sync.models.datamart import DataMart, DataMartGrant
from finance_sync.services.datamart_policy import (
    account_is_allowed,
    effective_grant,
)


def _datamart() -> DataMart:
    return DataMart(
        id="mart-1",
        tenant_id="tenant-1",
        key="portfolio-v1",
        display_name="Portfolio",
        dataset="portfolio",
        schema_version="pfc/1.0",
        fields=["account_id", "instrument_id", "quantity", "market_value"],
        delivery_method="pull_api",
        delivery_config={},
    )


def _grant(**overrides: object) -> DataMartGrant:
    values: dict[str, object] = {
        "id": "grant-1",
        "tenant_id": "tenant-1",
        "consumer_id": "consumer-1",
        "datamart_id": "mart-1",
        "household_scope": "explicit",
        "allowed_account_ids": ["account-1"],
        "allowed_fields": ["account_id", "quantity"],
    }
    values.update(overrides)
    return DataMartGrant(**values)


def test_grant_restricts_fields_and_accounts() -> None:
    policy = effective_grant(_datamart(), _grant())
    assert policy.fields == ("account_id", "quantity")
    assert account_is_allowed(
        policy, account_id="account-1", is_household_visible=False
    )
    assert not account_is_allowed(
        policy, account_id="account-2", is_household_visible=True
    )


def test_household_grant_allows_household_visible_accounts_only() -> None:
    policy = effective_grant(
        _datamart(), _grant(household_scope="household", allowed_account_ids=[])
    )
    assert account_is_allowed(
        policy, account_id="account-2", is_household_visible=True
    )
    assert not account_is_allowed(
        policy, account_id="account-2", is_household_visible=False
    )


def test_invalid_persisted_field_grant_fails_closed() -> None:
    policy = effective_grant(
        _datamart(), _grant(allowed_fields=["secret_note"])
    )
    assert policy.fields == ()


def test_datamart_request_rejects_unknown_delivery_method() -> None:
    with pytest.raises(ValidationError, match="delivery_method"):
        DataMartCreate(
            key="portfolio-v1",
            display_name="Portfolio",
            dataset="portfolio",
            schema_version="pfc/1.0",
            fields=["account_id"],
            delivery_method="email",
        )


def test_grant_request_rejects_unknown_household_scope() -> None:
    with pytest.raises(ValidationError, match="household_scope"):
        GrantCreate(
            consumer_id="consumer-1",
            datamart_id="mart-1",
            household_scope="all-accounts",
        )
