"""Small async client for the Firefly III v1 REST API."""

from __future__ import annotations

import asyncio
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
    retry_attempts: int = 3
    retry_base_delay: float = 0.5

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
        self._config = config
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

    async def ensure_category(self, name: str) -> dict[str, Any]:
        """Resolve or create a native Firefly category."""
        body = await self._request("GET", "/api/v1/categories")
        for item in _data(body):
            if str(item.get("name", "")).casefold() == name.casefold():
                return item
        return _single(
            await self._request(
                "POST", "/api/v1/categories", json={"name": name}
            )
        )

    async def ensure_tag(self, name: str) -> dict[str, Any]:
        """Resolve or create a native Firefly tag."""
        body = await self._request("GET", "/api/v1/tags")
        for item in _data(body):
            if (
                str(item.get("tag", item.get("name", ""))).casefold()
                == name.casefold()
            ):
                return item
        return _single(
            await self._request("POST", "/api/v1/tags", json={"tag": name})
        )

    async def ensure_budget(
        self,
        name: str,
        *,
        notes: str | None = None,
        auto_budget_amount: str | None = None,
        currency_code: str | None = None,
    ) -> dict[str, Any]:
        """Resolve or create a native Firefly budget."""
        body = await self._request("GET", "/api/v1/budgets")
        for item in _data(body):
            if str(item.get("name", "")).casefold() == name.casefold():
                return item
        payload: dict[str, Any] = {"name": name}
        if notes is not None:
            payload["notes"] = notes
        if auto_budget_amount is not None:
            payload["auto_budget_amount"] = auto_budget_amount
        if currency_code is not None:
            payload["auto_budget_currency_code"] = currency_code
        return _single(
            await self._request("POST", "/api/v1/budgets", json=payload)
        )

    async def ensure_bill(
        self,
        name: str,
        *,
        amount_min: str = "0",
        amount_max: str = "0",
        currency_code: str = "EUR",
        var_date: str | None = None,
        repeat_freq: str = "monthly",
    ) -> dict[str, Any]:
        """Resolve or create a native Firefly recurring bill."""
        body = await self._request("GET", "/api/v1/bills")
        for item in _data(body):
            if str(item.get("name", "")).casefold() == name.casefold():
                return item
        payload: dict[str, Any] = {
            "name": name,
            "amount_min": amount_min,
            "amount_max": amount_max,
            "currency_code": currency_code,
            "repeat_freq": repeat_freq,
        }
        if var_date is not None:
            payload["var_date"] = var_date
        return _single(
            await self._request("POST", "/api/v1/bills", json=payload)
        )

    async def store_transaction(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        remote_payload = dict(payload)
        canonical_splits = remote_payload.pop("canonical_splits", None)
        if isinstance(canonical_splits, list) and canonical_splits:
            native_splits: list[dict[str, Any]] = []
            split_items = cast(list[Any], canonical_splits)
            for index, raw_split in enumerate(split_items):
                if not isinstance(raw_split, dict):
                    continue
                split = cast(dict[str, Any], raw_split)
                line = {
                    key: remote_payload[key]
                    for key in (
                        "type",
                        "date",
                        "currency_code",
                        "source_name",
                        "destination_name",
                        "tags",
                        "reconciled",
                    )
                    if key in remote_payload
                }
                line.update(
                    {
                        "amount": str(split.get("amount", "0")),
                        "description": remote_payload.get("description"),
                        "external_id": (
                            f"{remote_payload.get('external_id')}:{index}"
                        ),
                        "notes": remote_payload.get("notes"),
                    }
                )
                if split.get("category"):
                    line["category_name"] = split["category"]
                native_splits.append(line)
            transactions: list[dict[str, Any]] = native_splits or [
                remote_payload
            ]
        else:
            transactions = [remote_payload]
        body = await self._request(
            "POST",
            "/api/v1/transactions",
            json={
                "error_if_duplicate_hash": True,
                "apply_rules": True,
                "fire_webhooks": False,
                "transactions": transactions,
            },
        )
        return _single(body)

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        attempts = max(1, self._config.retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if attempt == attempts:
                    msg = f"Firefly connection failed: {exc}"
                    raise FireflyClientError(msg) from exc
                response = None
            if response is not None:
                retryable = response.status_code in {408, 425, 429} or (
                    response.status_code >= 500
                )
                if not retryable or attempt == attempts:
                    break
            delay = self._config.retry_base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
        else:
            msg = "Firefly request retry loop exhausted"
            raise FireflyClientError(msg)
        assert response is not None
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
