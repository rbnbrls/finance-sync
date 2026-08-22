"""Release 13 contract tests for the securities read cleanup."""

from finance_sync.services.read.schemas import (
    CollectionMeta,
    SecurityInfo,
    SecurityListResponse,
    SecurityPriceListResponse,
    SecurityPriceResponse,
    TopLevelPriceListResponse,
)
from finance_sync.services.read.securities import SecuritiesReadService
from finance_sync.services.read_api import ReadService


def test_read_facade_has_no_legacy_security_sql() -> None:
    source = ReadService.__module__
    assert source == "finance_sync.services.read_api"

    import inspect

    facade_source = inspect.getsource(
        __import__(source, fromlist=["ReadService"])
    )
    assert "select(" not in facade_source
    assert "from finance_sync.models.security" not in facade_source


def test_security_component_owns_read_methods_and_schemas() -> None:
    assert ReadService.list_securities is SecuritiesReadService.list_securities
    assert (
        ReadService.get_security_prices
        is SecuritiesReadService.get_security_prices
    )
    assert ReadService.get_prices is SecuritiesReadService.get_prices

    response_types = (
        SecurityInfo,
        SecurityListResponse,
        SecurityPriceListResponse,
        SecurityPriceResponse,
        TopLevelPriceListResponse,
        CollectionMeta,
    )
    assert all(
        response_type.__module__.endswith("read.schemas")
        for response_type in response_types[:-1]
    )
    assert CollectionMeta.__module__.endswith("schemas.freshness")
