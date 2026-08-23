"""Small async client for Ghostfolio's documented import API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from finance_sync.exporter.ghostfolio.config import GhostfolioConfig

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport


class GhostfolioClientError(Exception):
    """Base Ghostfolio client error."""


class GhostfolioAuthError(GhostfolioClientError):
    """Ghostfolio rejected the bearer token."""


class GhostfolioAPIError(GhostfolioClientError):
    """Ghostfolio returned an unsuccessful response."""


class GhostfolioClient:
    """Bearer-token client for health checks and activity imports.

    Ghostfolio's import endpoint rejects a whole batch when one activity is
    a duplicate.  ``import_activities`` therefore sends rows individually,
    which makes retries safe and lets us distinguish duplicates from real
    failures.
    """

    def __init__(
        self,
        config: GhostfolioConfig,
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

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GhostfolioClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def health(self) -> dict[str, Any]:
        response = await self._client.get("/api/v1/health")
        if response.is_error:
            message = f"Ghostfolio health failed ({response.status_code})"
            raise GhostfolioAPIError(message)
        return response.json()

    async def import_activities(
        self, activities: list[dict[str, Any]], *, dry_run: bool = False
    ) -> dict[str, Any]:
        imported = skipped = 0
        failures: list[dict[str, Any]] = []
        for activity in activities:
            response = await self._client.post(
                "/api/v1/import",
                params={"dryRun": str(dry_run).lower()},
                json={"activities": [activity]},
            )
            if response.status_code in {200, 201}:
                imported += 1
                continue
            detail = response.text[:500]
            if response.status_code == 400 and "duplicate" in detail.lower():
                skipped += 1
                continue
            if response.status_code in {401, 403}:
                message = (
                    f"Ghostfolio authentication failed ({response.status_code})"
                )
                raise GhostfolioAuthError(message)
            failures.append(
                {
                    "activity": activity,
                    "status": response.status_code,
                    "detail": detail,
                }
            )
        return {
            "imported": imported,
            "skipped": skipped,
            "failed": len(failures),
            "failures": failures,
        }
