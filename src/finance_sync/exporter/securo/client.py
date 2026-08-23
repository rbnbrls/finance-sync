from __future__ import annotations

import json
from typing import Any

import httpx

from finance_sync.exporter.securo.config import SecuroConfig


class SecuroClient:
    """Small client for Securo's documented session-authenticated import API."""

    def __init__(self, config: SecuroConfig) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.server_url.rstrip("/"),
            timeout=config.request_timeout,
            verify=config.verify_ssl,
        )
        self._token: str | None = None

    async def __aenter__(self) -> SecuroClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            message = "Securo client is not authenticated"
            raise RuntimeError(message)
        return {"Authorization": f"Bearer {self._token}"}

    async def login(self) -> None:
        response = await self._client.post(
            "/api/auth/login",
            data={
                "username": self.config.email,
                "password": self.config.password,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("requires_2fa"):
            message = (
                "Securo login requires 2FA; disable 2FA for the local test user"
            )
            raise RuntimeError(message)
        self._token = payload["access_token"]

    async def accounts(self) -> list[dict[str, Any]]:
        response = await self._client.get(
            "/api/accounts", headers=self._auth_headers()
        )
        response.raise_for_status()
        return response.json()

    async def create_account(
        self, *, name: str, currency: str, account_type: str
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/api/accounts",
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
            },
            json={
                "name": name,
                "type": account_type,
                "currency": currency,
                "balance": "0.00",
            },
        )
        response.raise_for_status()
        return response.json()

    async def import_csv(
        self,
        *,
        content: bytes,
        filename: str,
        account_id: str,
        mapping: dict[str, str],
    ) -> dict[str, Any]:
        preview = await self._client.post(
            "/api/transactions/import/preview",
            headers=self._auth_headers(),
            files={"file": (filename, content, "text/csv")},
            data={
                "date_format": "YYYY-MM-DD",
                "column_mapping": json.dumps(mapping),
            },
        )
        preview.raise_for_status()
        preview_payload = preview.json()
        transactions = preview_payload.get("transactions", [])
        if preview_payload.get("parse_error") or not transactions:
            detail = preview_payload.get("parse_error", "no transactions")
            message = f"Securo import preview failed: {detail}"
            raise RuntimeError(message)
        imported = await self._client.post(
            "/api/transactions/import",
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
            },
            json={
                "account_id": account_id,
                "transactions": transactions,
                "filename": filename,
                "detected_format": "csv",
                "detect_duplicates": True,
            },
        )
        imported.raise_for_status()
        return {"preview": preview_payload, "import": imported.json()}
