"""Async client for InvestBrain's documented Sanctum API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import httpx

from finance_sync.exporter.investbrain.config import InvestBrainConfig

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport


class InvestBrainClientError(Exception):
    """Base InvestBrain client error."""


class InvestBrainAuthError(InvestBrainClientError):
    """InvestBrain rejected the token."""


class InvestBrainAPIError(InvestBrainClientError):
    """InvestBrain returned an unsuccessful response."""


class InvestBrainClient:
    """Authenticated client for portfolios and transactions.

    InvestBrain's JSON API does not expose an external-id field on create.
    Upserts therefore use the stable transaction UUID when available and a
    deterministic business fingerprint as a fallback for already imported
    rows.
    """

    def __init__(
        self,
        config: InvestBrainConfig,
        transport: AsyncBaseTransport | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "base_url": config.server_url.rstrip("/"),
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

    async def __aenter__(self) -> InvestBrainClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        response = await self._client.get("/up")
        if response.is_error:
            message = f"InvestBrain health failed ({response.status_code})"
            raise InvestBrainAPIError(message)
        return {"status": "ok", "status_code": response.status_code}

    async def list_portfolios(self) -> list[dict[str, Any]]:
        return await self._list("/api/portfolio")

    async def list_transactions(self) -> list[dict[str, Any]]:
        return await self._list("/api/transaction")

    async def create_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/portfolio", json=payload)

    async def update_portfolio(
        self, portfolio_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", f"/api/portfolio/{portfolio_id}", json=payload
        )

    async def create_transaction(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request("POST", "/api/transaction", json=payload)

    async def update_transaction(
        self, transaction_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT", f"/api/transaction/{transaction_id}", json=payload
        )

    async def upsert_transaction(
        self, payload: dict[str, Any], existing: list[dict[str, Any]]
    ) -> str:
        fingerprint = _fingerprint(payload)
        for row in existing:
            if _fingerprint(row) == fingerprint:
                return "skipped"
        await self.create_transaction(payload)
        return "created"

    async def _list(self, path: str) -> list[dict[str, Any]]:
        page = 1
        rows: list[dict[str, Any]] = []
        while True:
            body = await self._request(
                "GET", path, params={"page": page, "itemsPerPage": 100}
            )
            data = body.get("data", [])
            if not isinstance(data, list):
                break
            rows.extend(cast(list[dict[str, Any]], data))
            meta = cast(dict[str, Any], body.get("meta", {}))
            if page >= int(meta.get("last_page", page)):
                break
            page += 1
        return rows

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            message = f"InvestBrain connection failed: {exc}"
            raise InvestBrainClientError(message) from exc
        if response.status_code in {401, 403}:
            message = (
                f"InvestBrain authentication failed ({response.status_code})"
            )
            raise InvestBrainAuthError(message)
        if response.is_error:
            message = (
                f"InvestBrain API {response.status_code}: {response.text[:500]}"
            )
            raise InvestBrainAPIError(message)
        if response.status_code == 204 or not response.content:
            return {}
        value = response.json()
        return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(key, ""))
        for key in (
            "symbol",
            "portfolio_id",
            "transaction_type",
            "quantity",
            "cost_basis",
            "sale_price",
            "date",
        )
    )
