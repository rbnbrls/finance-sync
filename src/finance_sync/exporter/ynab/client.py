"""Small async YNAB API client used by the native exporter."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import httpx

from finance_sync.exporter.ynab.config import YNABConfig


class YNABAPIError(RuntimeError):
    """A non-successful YNAB API response."""


class YNABClient:
    """Transport-only client; mapping stays in the exporter."""

    def __init__(
        self,
        config: YNABConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        if http_client is None:
            self._client = httpx.AsyncClient(
                base_url=config.api_base_url,
                headers={
                    "Authorization": f"Bearer {config.access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        else:
            http_client.headers.update(
                {
                    "Authorization": f"Bearer {config.access_token}",
                    "Content-Type": "application/json",
                }
            )
            self._client = http_client
        self._owns_client = http_client is None

    async def __aenter__(self) -> YNABClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_accounts(self) -> list[dict[str, Any]]:
        return await self._get(f"/budgets/{self.config.budget_id}/accounts")

    async def get_categories(self) -> list[dict[str, Any]]:
        return await self._get(f"/budgets/{self.config.budget_id}/categories")

    async def get_transactions(
        self, since: str | None = None
    ) -> list[dict[str, Any]]:
        path = f"/budgets/{self.config.budget_id}/transactions"
        return await self._get(
            path, params={"since_date": since} if since else None
        )

    async def import_transactions(
        self, transactions: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/budgets/{self.config.budget_id}/transactions",
            json={"transactions": list(transactions)},
        )
        return await self._decode(response)

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._request("GET", path, params=params)
        payload = await self._decode(response)
        data = payload.get("data", payload)
        if isinstance(data, dict):
            data = cast("dict[str, Any]", data)
            for key in ("accounts", "categories", "transactions"):
                value: Any = data.get(key)
                if isinstance(value, list):
                    return cast("list[dict[str, Any]]", value)
        if isinstance(data, list):
            return cast("list[dict[str, Any]]", data)
        return []

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Perform an idempotent-key-safe request with bounded backoff."""
        attempts = max(1, self.config.retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError:
                if attempt == attempts:
                    raise
            else:
                retryable = response.status_code in {408, 425, 429} or (
                    response.status_code >= 500
                )
                if not retryable:
                    return response
                if attempt == attempts:
                    return response
            delay = self.config.retry_base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
        msg = "unreachable retry loop"
        raise AssertionError(msg)

    @staticmethod
    async def _decode(response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            message = (
                f"YNAB API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
            raise YNABAPIError(message)
        payload: Any = response.json()
        if not isinstance(payload, dict):
            message = "YNAB API returned a non-object response"
            raise YNABAPIError(message)
        return cast("dict[str, Any]", payload)
