"""Canonical action metadata exposed by the control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from finance_sync.schemas.control_plane import ControlPlaneAction


@dataclass(frozen=True, slots=True)
class ActionSpec:
    key: str
    label: str
    method: Literal["GET", "POST", "PUT", "PATCH"]
    permission: str
    destructive: bool = False


ACTION_CATALOG: dict[str, ActionSpec] = {
    "test_connection": ActionSpec(
        "test_connection", "Verbinding testen", "POST", "connectors:write"
    ),
    "sync_connection": ActionSpec(
        "sync_connection", "Nu synchroniseren", "POST", "sync:write"
    ),
    "view_connection": ActionSpec(
        "view_connection", "Details bekijken", "GET", "connectors:read"
    ),
    "view_accounts": ActionSpec(
        "view_accounts", "Accounts bekijken", "GET", "accounts:read"
    ),
    "view_transactions": ActionSpec(
        "view_transactions", "Transacties bekijken", "GET", "transactions:read"
    ),
    "view_holdings": ActionSpec(
        "view_holdings", "Posities bekijken", "GET", "holdings:read"
    ),
    "view_imports": ActionSpec(
        "view_imports", "Importdetails bekijken", "GET", "connectors:read"
    ),
    "open_file_import": ActionSpec(
        "open_file_import", "Bestanden uploaden", "GET", "connectors:read"
    ),
    "edit_connection": ActionSpec(
        "edit_connection", "Bewerken", "GET", "connectors:read"
    ),
    "pause_connection": ActionSpec(
        "pause_connection", "Pauzeren", "POST", "connectors:write"
    ),
    "view_sync_run": ActionSpec(
        "view_sync_run", "Sync-details bekijken", "GET", "sync:read"
    ),
    "retry_sync": ActionSpec(
        "retry_sync", "Synchronisatie opnieuw proberen", "POST", "sync:write"
    ),
    "map_security": ActionSpec(
        "map_security", "Security mappen", "PUT", "securities:write"
    ),
    "view_data_source": ActionSpec(
        "view_data_source", "Bron bekijken", "GET", "enrichment:read"
    ),
    "refresh_quotes": ActionSpec(
        "refresh_quotes", "Koersen bijwerken", "POST", "enrichment:write"
    ),
    "accept_unavailable_quotes": ActionSpec(
        "accept_unavailable_quotes",
        "Ontbrekende koersen accepteren",
        "POST",
        "enrichment:write",
    ),
    "test_destination": ActionSpec(
        "test_destination", "Bestemming testen", "POST", "destinations:write"
    ),
    "preview_destination": ActionSpec(
        "preview_destination", "Preview bekijken", "POST", "destinations:read"
    ),
    "configure_destination": ActionSpec(
        "configure_destination",
        "Bestemming configureren",
        "GET",
        "destinations:read",
    ),
    "pause_destination": ActionSpec(
        "pause_destination", "Bestemming pauzeren", "POST", "destinations:write"
    ),
    "run_export": ActionSpec(
        "run_export", "Export uitvoeren", "POST", "destinations:write", True
    ),
    "retry_export": ActionSpec(
        "retry_export",
        "Export opnieuw proberen",
        "POST",
        "destinations:write",
        True,
    ),
    "view_export": ActionSpec(
        "view_export", "Export bekijken", "GET", "destinations:read"
    ),
    "view_reconciliation": ActionSpec(
        "view_reconciliation", "Finding bekijken", "GET", "reconciliation:read"
    ),
}


def action(
    key: str,
    path: str,
    *,
    permissions: set[str] | None = None,
    disabled_reason: str | None = None,
) -> ControlPlaneAction:
    """Build a validated action from the allow-listed catalog."""
    spec = ACTION_CATALOG[key]
    permission_enabled = permissions is None or _has_permission(
        permissions, spec.permission
    )
    enabled = permission_enabled and disabled_reason is None
    return ControlPlaneAction(
        key=spec.key,
        label=spec.label,
        method=spec.method,
        path=path,
        permission=spec.permission,
        destructive=spec.destructive,
        enabled=enabled,
        disabled_reason=(
            None
            if enabled
            else disabled_reason or f"Ontbrekende permissie: {spec.permission}"
        ),
    )


def _has_permission(permissions: set[str], required: str) -> bool:
    resource, _, operation = required.partition(":")
    return (
        "*:*" in permissions
        or required in permissions
        or f"{resource}:*" in permissions
        or (operation == "read" and f"{resource}:read" in permissions)
    )
