"""Export artifact cleanup — user-confirmed quarantine/delete after revoke.

When an account's visibility is revoked (``household`` → ``private``),
the exporters stop including it on the next run — but data that was
*already* exported (Wealthfolio account mapping rows, delivery cursors,
CSV files on disk) is not touched by the visibility change itself.
Deleting or quarantining that data is a **user-confirmed** decision:

* :func:`describe_export_artifacts` — what exists for this account.
* :func:`quarantine_export_artifacts` — non-destructive: moves the CSV
  files into ``<output_dir>/quarantine/<account_id>/`` and keeps the
  mapping/delivery rows, so a future re-share resumes cleanly.
* :func:`delete_export_artifacts` — destructive: removes the CSV files
  (including any quarantined copies) and the mapping/delivery rows.
  Requires explicit ``confirm=True``; refusing confirmation raises
  ``AccountSharingError(ERR_CONFIRMATION_REQUIRED)`` and touches nothing.

Every decision is recorded in the tenant-scoped household audit log as
``account_export_quarantine`` with a sanitised payload (account ids,
file counts — never financial data).  There is **no silent deletion**:
the unshare endpoint only reports that cleanup is required, and the
owner must actively confirm quarantine or deletion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models.household_audit_log import (
    AUDIT_ACCOUNT_EXPORT_QUARANTINE,
    HouseholdAuditLog,
)
from finance_sync.services.account_sharing import (
    AccountSharingError,
    get_account_for_management,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.user import User

#: Subdirectory under the exporter output dir where quarantined CSV
#: files are moved (per-account subdirectory underneath).
QUARANTINE_DIRNAME = "quarantine"

#: Stable error code for a destructive action without confirmation.
ERR_CONFIRMATION_REQUIRED = "confirmation_required"


def _sanitize_file_prefix(name: str) -> str:
    """Mirror the exporter's CSV filename sanitisation.

    Kept in sync with ``WealthfolioExporter._write_csv_file`` so the
    cleanup flow finds exactly the files the exporter wrote.
    """
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def _find_csv_files(output_dir: Path, wf_account_name: str) -> list[Path]:
    """Return the CSV files in *output_dir* belonging to one account.

    Matches ``transactions_<safe>_<ts>.csv`` / ``holdings_<safe>_<ts>.csv``
    — the naming the exporter uses (safe = sanitised account name).
    """
    safe = _sanitize_file_prefix(wf_account_name)
    prefixes = (f"transactions_{safe}_", f"holdings_{safe}_")
    if not output_dir.is_dir():
        return []
    return sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and path.suffix == ".csv"
        and path.name.startswith(prefixes)
    )


async def describe_export_artifacts(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
    output_dir: Path,
) -> dict[str, Any]:
    """Describe the exported artifacts for *account_id*.

    Ownership is enforced via :func:`get_account_for_management`
    (owner, or admin for system-owned accounts).  Returns the mapping
    row, the delivery cursor and the CSV files the exporter wrote for
    this account — without modifying anything.
    """
    account = await get_account_for_management(
        session, tenant_id=tenant_id, account_id=account_id, actor=actor
    )

    mapping_result = await session.execute(
        select(WealthfolioAccountMapping).where(
            WealthfolioAccountMapping.tenant_id == tenant_id,
            WealthfolioAccountMapping.account_id == account_id,
        )
    )
    mapping = mapping_result.scalar_one_or_none()

    delivery_result = await session.execute(
        select(WealthfolioDelivery).where(
            WealthfolioDelivery.tenant_id == tenant_id,
            WealthfolioDelivery.account_id == account_id,
        )
    )
    delivery = delivery_result.scalar_one_or_none()

    wf_account_name = (
        mapping.wf_account_name if mapping is not None else account.name
    )
    csv_files = _find_csv_files(output_dir, wf_account_name)
    quarantine_dir = output_dir / QUARANTINE_DIRNAME / account_id
    quarantined_files = (
        sorted(quarantine_dir.glob("*.csv")) if quarantine_dir.is_dir() else []
    )

    return {
        "account_id": str(account.id),
        "account_name": account.name,
        "has_mapping": mapping is not None,
        "wf_account_name": wf_account_name,
        "has_delivery_cursor": delivery is not None,
        "last_exported_at": (
            delivery.last_exported_at.isoformat()
            if delivery is not None and delivery.last_exported_at is not None
            else None
        ),
        "csv_file_count": len(csv_files),
        "csv_files": [str(p) for p in csv_files],
        "quarantined_file_count": len(quarantined_files),
    }


def _audit_quarantine(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: User,
    detail: dict[str, Any],
) -> None:
    """Record an export-quarantine decision in the household audit log."""
    session.add(
        HouseholdAuditLog(
            tenant_id=tenant_id,
            action=AUDIT_ACCOUNT_EXPORT_QUARANTINE,
            detail=detail,
            actor_user_id=str(actor.id),
            actor_role=actor.role,
        )
    )


async def quarantine_export_artifacts(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
    output_dir: Path,
) -> dict[str, Any]:
    """Move the account's CSV files into the quarantine directory.

    Non-destructive: files are moved (not deleted) under
    ``<output_dir>/quarantine/<account_id>/`` and the mapping/delivery
    rows are kept, so a later re-share resumes from where it stopped.
    Returns the number of files quarantined and the target directory.
    """
    account = await get_account_for_management(
        session, tenant_id=tenant_id, account_id=account_id, actor=actor
    )

    mapping_result = await session.execute(
        select(WealthfolioAccountMapping).where(
            WealthfolioAccountMapping.tenant_id == tenant_id,
            WealthfolioAccountMapping.account_id == account_id,
        )
    )
    mapping = mapping_result.scalar_one_or_none()
    wf_account_name = (
        mapping.wf_account_name if mapping is not None else account.name
    )
    csv_files = _find_csv_files(output_dir, wf_account_name)

    quarantine_dir = output_dir / QUARANTINE_DIRNAME / account_id
    moved = 0
    for path in csv_files:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / path.name
        if not target.exists():
            path.rename(target)
        else:
            path.unlink(missing_ok=True)
        moved += 1

    _audit_quarantine(
        session,
        tenant_id=tenant_id,
        actor=actor,
        detail={
            "account_id": str(account.id),
            "account_name": account.name,
            "wf_account_name": wf_account_name,
            "decision": "quarantine",
            "file_count": moved,
            "quarantine_dir": str(quarantine_dir),
        },
    )
    await session.flush()
    return {
        "account_id": str(account.id),
        "quarantined_files": moved,
        "quarantine_dir": str(quarantine_dir),
    }


async def delete_export_artifacts(
    session: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    actor: User,
    output_dir: Path,
    confirm: bool,
) -> dict[str, Any]:
    """Permanently remove the account's exported artifacts.

    Destructive: deletes the CSV files (including quarantined copies)
    and the Wealthfolio mapping/delivery rows.  Requires explicit
    ``confirm=True``; otherwise raises
    ``AccountSharingError(ERR_CONFIRMATION_REQUIRED)`` and touches
    nothing.  Audited as ``account_export_quarantine`` (decision=delete).
    """
    if not confirm:
        msg = (
            "Deleting exported data is destructive; pass confirm=true "
            "to proceed"
        )
        raise AccountSharingError(ERR_CONFIRMATION_REQUIRED, msg)

    account = await get_account_for_management(
        session, tenant_id=tenant_id, account_id=account_id, actor=actor
    )

    mapping_result = await session.execute(
        select(WealthfolioAccountMapping).where(
            WealthfolioAccountMapping.tenant_id == tenant_id,
            WealthfolioAccountMapping.account_id == account_id,
        )
    )
    mapping = mapping_result.scalar_one_or_none()
    wf_account_name = (
        mapping.wf_account_name if mapping is not None else account.name
    )

    csv_files = _find_csv_files(output_dir, wf_account_name)
    quarantine_dir = output_dir / QUARANTINE_DIRNAME / account_id
    quarantined_files = (
        sorted(quarantine_dir.glob("*.csv")) if quarantine_dir.is_dir() else []
    )

    removed = 0
    for path in csv_files:
        path.unlink(missing_ok=True)
        removed += 1
    for path in quarantined_files:
        path.unlink(missing_ok=True)
        removed += 1

    deleted_mapping = False
    if mapping is not None:
        await session.delete(mapping)
        deleted_mapping = True
    if quarantined_files and not list(quarantine_dir.iterdir()):
        quarantine_dir.rmdir()

    delivery_result = await session.execute(
        select(WealthfolioDelivery).where(
            WealthfolioDelivery.tenant_id == tenant_id,
            WealthfolioDelivery.account_id == account_id,
        )
    )
    delivery = delivery_result.scalar_one_or_none()
    deleted_delivery = False
    if delivery is not None:
        await session.delete(delivery)
        deleted_delivery = True

    _audit_quarantine(
        session,
        tenant_id=tenant_id,
        actor=actor,
        detail={
            "account_id": str(account.id),
            "account_name": account.name,
            "wf_account_name": wf_account_name,
            "decision": "delete",
            "file_count": removed,
            "deleted_mapping": deleted_mapping,
            "deleted_delivery": deleted_delivery,
        },
    )
    await session.flush()
    return {
        "account_id": str(account.id),
        "deleted_files": removed,
        "deleted_mapping": deleted_mapping,
        "deleted_delivery": deleted_delivery,
    }
