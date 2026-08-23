"""Small async client for the Firefly III v1 REST API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport


class FireflyClientError(Exception):
    """Base Firefly client error."""


class FireflyAuthError(FireflyClientError):
    """Missing or invalid access token."""


class FireflyAPIError(FireflyClientError):
    """Firefly returned an unsuccessful response."""


@dataclass
class FireflyClientConfig:
    base_url: str
    access_token: str
    request_timeout: float = 60.0
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        if not self.base_url:
            msg = "base_url must be non-empty"
            raise ValueError(msg)
        if not self.access_token:
            msg = "access_token must be non-empty"
            raise ValueError(msg)


class FireflyClient:
    """Authenticated Firefly III API client with testable HTTP transport."""

    def __init__(
        self,
        config: FireflyClientConfig,
        transport: AsyncBaseTransport | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "base_url": config.base_url.rstrip("/"),
            "timeout": config.request_timeout,
            "verify": config.verify_ssl,
            "headers": {
                "Authorization": f"Bearer {config.access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> FireflyClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def about(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/about")

    async def list_accounts(self) -> list[dict[str, Any]]:
        body = await self._request(
            "GET", "/api/v1/accounts", params={"type": "asset"}
        )
        return _data(body)

    async def create_asset_account(
        self, *, name: str, currency_code: str
    ) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "/api/v1/accounts",
            json={
                "name": name,
                "type": "asset",
                "account_role": "sharedAsset",
                "currency_code": currency_code,
                "active": True,
                "include_net_worth": True,
            },
        )
        return _single(body)

    async def ensure_asset_account(
        self, *, name: str, currency_code: str
    ) -> dict[str, Any]:
        for account in await self.list_accounts():
            if str(account.get("name", "")).casefold() == name.casefold():
                return account
        return await self.create_asset_account(
            name=name, currency_code=currency_code
        )

    async def store_transaction(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "/api/v1/transactions",
            json={
                "error_if_duplicate_hash": True,
                "apply_rules": True,
                "fire_webhooks": False,
                "transactions": [payload],
            },
        )
        return _single(body)

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            msg = f"Firefly connection failed: {exc}"
            raise FireflyClientError(msg) from exc
        if response.status_code in {401, 403}:
            msg = f"Firefly authentication failed ({response.status_code})"
            raise FireflyAuthError(msg)
        if response.is_error:
            detail = response.text[:500]
            msg = f"Firefly API {response.status_code}: {detail}"
            raise FireflyAPIError(msg)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()


def _data(body: dict[str, Any]) -> list[dict[str, Any]]:
    value = body.get("data", body)
    if not isinstance(value, list):
        return []
    items = cast(list[Any], value)
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        record = cast(dict[str, Any], item)
        attributes = record.get("attributes")
        if isinstance(attributes, dict):
            normalized.append(
                {"id": record.get("id"), **cast(dict[str, Any], attributes)}
            )
        else:
            normalized.append(record)
    return normalized


def _single(body: dict[str, Any]) -> dict[str, Any]:
    value = body.get("data", body)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}
