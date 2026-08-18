"""Policy evaluation for governed datamart consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finance_sync.models.datamart import DataMart, DataMartGrant


@dataclass(frozen=True, slots=True)
class EffectiveDataMartGrant:
    """The non-secret policy a consumer is allowed to use."""

    datamart_key: str
    dataset: str
    schema_version: str
    fields: tuple[str, ...]
    delivery_method: str
    household_scope: str
    allowed_account_ids: tuple[str, ...]


def effective_grant(
    datamart: DataMart, grant: DataMartGrant
) -> EffectiveDataMartGrant:
    """Return a fail-closed effective grant.

    A grant may only reduce the datamart field surface. An invalid stored grant
    has no effective fields and therefore exposes no records until corrected.
    """
    mart_fields = tuple(datamart.fields or [])
    requested = tuple(grant.allowed_fields or [])
    fields = (
        mart_fields
        if not requested
        else tuple(field for field in requested if field in mart_fields)
    )
    return EffectiveDataMartGrant(
        datamart_key=datamart.key,
        dataset=datamart.dataset,
        schema_version=datamart.schema_version,
        fields=fields,
        delivery_method=datamart.delivery_method,
        household_scope=grant.household_scope,
        allowed_account_ids=tuple(grant.allowed_account_ids or []),
    )


def account_is_allowed(
    policy: EffectiveDataMartGrant,
    *,
    account_id: str,
    is_household_visible: bool,
) -> bool:
    """Evaluate account scope before a projection is delivered."""
    if account_id in policy.allowed_account_ids:
        return True
    return policy.household_scope == "household" and is_household_visible
