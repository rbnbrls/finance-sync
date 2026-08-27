"""Tenant-scoped provider health projection.

The service is read-only and intentionally does not decrypt credentials or
call providers.  ``connection`` describes authentication/configuration,
``resources`` describes source and freshness state, and
``last_successful_processing`` describes the ingestion result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select

from finance_sync.models import Credential, SyncRun
from finance_sync.schemas.provider_health import (
    ProviderConnectionHealth,
    ProviderHealthOverview,
    ProviderHealthStatus,
    ProviderProcessingHealth,
    ProviderResourceHealth,
)
from finance_sync.schemas.rate_limit import RateLimitDiagnosis
from finance_sync.services.connector_compatibility import (
    default_contract_paths,
    evaluate_connector,
    load_json,
)
from finance_sync.utils.redaction import sanitize_error


class ProviderHealthService:
    """Build canonical provider health for every connection in a tenant."""

    def __init__(
        self,
        session: Any,
        tenant_id: str,
        *,
        registry: Any = None,
        freshness_limit: timedelta = timedelta(hours=24),
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._registry = registry
        self._freshness_limit = freshness_limit
        self._now = now or datetime.now(UTC)

    async def get_overview(self) -> list[ProviderHealthOverview]:
        credentials = list(
            (
                await self._session.execute(
                    select(Credential)
                    .where(Credential.tenant_id == self._tenant_id)
                    .order_by(Credential.created_at)
                )
            ).scalars()
        )
        connection_ids = [str(row.id) for row in credentials]
        runs = await self._load_runs(connection_ids)
        catalog = self._catalog()
        lifecycle_path, matrix_path = default_contract_paths()
        lifecycle = load_json(lifecycle_path)
        matrix = load_json(matrix_path)
        fixture_versions = {
            str(item["name"]): str(item["fixture_date"])
            for item in cast(
                "list[dict[str, Any]]", matrix.get("connectors", [])
            )
            if item.get("name") and item.get("fixture_date")
        }

        return [
            self._connection_health(
                credential,
                runs.get(str(credential.id), []),
                catalog.get(credential.provider_key, {}),
                lifecycle,
                matrix,
                fixture_versions.get(credential.provider_key),
            )
            for credential in credentials
        ]

    async def _load_runs(
        self, connection_ids: list[str]
    ) -> dict[str, list[SyncRun]]:
        if not connection_ids:
            return {}
        rows = (
            await self._session.execute(
                select(SyncRun)
                .join(
                    Credential,
                    Credential.id == SyncRun.connection_id,
                )
                .where(
                    Credential.tenant_id == self._tenant_id,
                    SyncRun.connection_id.in_(connection_ids),
                )
                .order_by(SyncRun.started_at.desc())
            )
        ).scalars()
        grouped: dict[str, list[SyncRun]] = {}
        for row in rows:
            grouped.setdefault(str(row.connection_id), []).append(row)
        return grouped

    def _catalog(self) -> dict[str, dict[str, Any]]:
        if self._registry is None:
            from finance_sync.connectors.registry import ConnectorRegistry

            self._registry = ConnectorRegistry()
        return self._registry.list_connectors()

    def _connection_health(
        self,
        credential: Credential,
        runs: list[SyncRun],
        metadata: dict[str, Any],
        lifecycle: dict[str, Any],
        matrix: dict[str, Any],
        fixture_version: str | None,
    ) -> ProviderHealthOverview:
        compatibility = evaluate_connector(
            lifecycle,
            metadata,
            fixture_version=fixture_version,
            contract_matrix=matrix,
        )
        connection = self._connection(credential)
        supported = sorted(
            str(resource)
            for resource in metadata.get("supported_resources", [])
        )
        resources = [
            self._resource_health(resource, runs) for resource in supported
        ]
        processing = self._processing_health(resources)
        overall_status, action_required = self._overall_status(
            credential,
            connection,
            resources,
            processing,
            compatibility.status,
        )
        return ProviderHealthOverview(
            connection_id=str(credential.id),
            provider=credential.provider_key,
            overall_status=overall_status,
            connection=connection,
            resources=resources,
            last_successful_processing=processing,
            compatibility=compatibility,
            action_required=action_required,
            evaluated_at=self._now,
        )

    @staticmethod
    def _connection(credential: Credential) -> ProviderConnectionHealth:
        retry_after_at = getattr(credential, "retry_after_at", None)
        expires_at = getattr(credential, "expires_at", None)
        reauth_required_at = getattr(credential, "reauth_required_at", None)
        credential_status = (
            getattr(credential, "credential_status", None) or "unknown"
        )
        expired = expires_at is not None and expires_at <= datetime.now(UTC)
        configured = bool(credential.encrypted_payload) or (
            credential.provider_key
            in {
                "csv_import",
                "degiro_pension",
                "manual_expense",
                "saxo_investor",
            }
        )
        if credential.status == "paused":
            status = "paused"
        elif expired or credential_status == "reauth_required":
            status = "reauth_required"
        elif credential.last_error:
            status = "error"
        elif credential.last_test_status == "success":
            status = "connected"
        elif configured:
            status = "configured"
        else:
            status = "not_configured"
        error_code = credential.last_error_category
        message = sanitize_error(credential.last_error or "") or None
        return ProviderConnectionHealth(
            status=status,
            credential_status=(
                "reauth_required"
                if expired
                else (credential_status if configured else "missing")
            ),
            auth_status=(
                "failed"
                if credential.last_error_category == "authentication"
                else (
                    "verified"
                    if credential.last_test_status == "success"
                    else "unknown"
                )
            ),
            checked_at=credential.last_test_at,
            last_test_at=credential.last_test_at,
            error_code=error_code,
            message=message,
            expires_at=expires_at,
            reauth_required_at=reauth_required_at,
            rate_limit=RateLimitDiagnosis(
                active=bool(
                    retry_after_at and retry_after_at > datetime.now(UTC)
                ),
                limited_at=getattr(credential, "rate_limited_at", None),
                retry_after_at=retry_after_at,
                attempt_count=int(
                    getattr(credential, "rate_limit_attempts", 0) or 0
                ),
                limit_scope=getattr(credential, "rate_limit_scope", None),
                last_http_status=getattr(credential, "last_http_status", None),
                action=(
                    "retry_now"
                    if not retry_after_at or retry_after_at <= datetime.now(UTC)
                    else "wait"
                ),
            ),
        )

    def _resource_health(
        self, resource: str, runs: list[SyncRun]
    ) -> ProviderResourceHealth:
        scoped_runs = [
            run
            for run in runs
            if getattr(run, "resource", None) in (None, resource)
        ]
        latest = scoped_runs[0] if scoped_runs else None
        latest_success = next(
            (run for run in scoped_runs if str(run.status) == "completed"), None
        )
        success_at = latest_success.completed_at if latest_success else None
        fresh_until = success_at + self._freshness_limit if success_at else None
        stale = fresh_until is None or fresh_until < self._now
        if latest is None:
            source_status = "not_processed"
        elif str(latest.status) == "completed" and not stale:
            source_status = "healthy"
        elif str(latest.status) == "completed":
            source_status = "stale"
        else:
            source_status = "failed"
        return ProviderResourceHealth(
            resource=resource,
            source_status=source_status,
            last_attempt_at=latest.started_at if latest else None,
            last_success_at=success_at,
            fresh_until=fresh_until,
            items_processed=int(latest_success.items_processed or 0)
            if latest_success
            else 0,
            sync_run_id=str(latest_success.id) if latest_success else None,
            error_category=(latest.error_category if latest else None),
            stale=stale,
        )

    @staticmethod
    def _processing_health(
        resources: list[ProviderResourceHealth],
    ) -> ProviderProcessingHealth:
        successful = [
            item for item in resources if item.last_success_at is not None
        ]
        latest = max(
            successful,
            key=lambda item: (
                item.last_success_at or datetime.min.replace(tzinfo=UTC)
            ),
            default=None,
        )
        return ProviderProcessingHealth(
            last_success_at=latest.last_success_at if latest else None,
            sync_run_id=latest.sync_run_id if latest else None,
            resources_processed=len(successful),
            all_supported_resources_processed=bool(resources)
            and len(successful) == len(resources),
        )

    @staticmethod
    def _overall_status(
        credential: Credential,
        connection: ProviderConnectionHealth,
        resources: list[ProviderResourceHealth],
        processing: ProviderProcessingHealth,
        compatibility_status: str,
    ) -> tuple[ProviderHealthStatus, str | None]:
        if credential.status == "paused":
            return "paused", "resume_connection"
        if compatibility_status == "incompatible":
            return "incompatible", "upgrade_connector"
        if compatibility_status == "unavailable":
            return "unavailable", "review_connector"
        if compatibility_status in {"attention_required", "deprecated"}:
            return "attention_required", "review_connector"
        if connection.status == "reauth_required":
            return "error", "reauthenticate"
        if connection.status in {"not_configured", "error"}:
            return "error", "test_connection"
        if any(item.source_status == "failed" for item in resources):
            return "error", "run_sync"
        if not processing.last_success_at:
            return "attention_required", "run_sync"
        if any(item.stale for item in resources):
            return "attention_required", "run_sync"
        return "healthy", None
