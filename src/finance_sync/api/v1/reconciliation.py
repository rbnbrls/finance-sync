"""Reconciliation API endpoints — trigger runs and view findings."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import (
    Decimal,
)
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_read_scope,
    require_permission,
)
from finance_sync.db.uow import UnitOfWork
from finance_sync.dependencies import get_container, get_db
from finance_sync.models import (
    Account,
    DuplicateReview,
    Security,
    Transaction,
    TransactionLifecycleEvent,
)
from finance_sync.services.reconciliation import ReconciliationService
from finance_sync.services.visibility import ReadScope
from finance_sync.sync.persistence import TransactionPersistence

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


# ── Request / Response DTOs ───────────────────────────────────────────


class ReconciliationTriggerRequest(BaseModel):
    """Request body to trigger a new reconciliation run."""

    account_ids: list[str] | None = Field(
        default=None,
        description="Optional subset of account IDs to analyze",
    )
    date_from: datetime | None = Field(
        default=None,
        description="Earliest transaction date (default 90 days ago)",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Latest transaction date (default now)",
    )
    threshold_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        description="Max hour gap for duplicate candidates",
    )


class TriggerReconciliationRequest(BaseModel):
    """Trigger a reconciliation with optional connector comparison."""

    connector_a: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "First connector/provider key (e.g. 'bunq', 'trading212'). "
            "When provided together with connector_b, triggers a "
            "targeted comparison between the two connectors."
        ),
    )
    connector_b: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Second connector/provider key (e.g. 'bunq', 'trading212'). "
            "Must be different from connector_a."
        ),
    )
    detect_duplicates: bool = Field(
        default=True,
        description="Whether to scan for duplicate transactions",
    )
    account_ids: list[str] | None = Field(
        default=None,
        description="Optional subset of account IDs to analyze",
    )
    date_from: datetime | None = Field(
        default=None,
        description="Earliest transaction date (default 90 days ago)",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Latest transaction date (default now)",
    )
    threshold_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        description="Max hour gap for duplicate candidates",
    )


class CompareConnectorsRequest(BaseModel):
    """Request body to compare two specific connectors/providers."""

    connector_a: str = Field(
        ...,
        min_length=1,
        description="First connector/provider key (e.g. 'bunq', 'trading212')",
    )
    connector_b: str = Field(
        ...,
        min_length=1,
        description="Second connector/provider key (e.g. 'bunq', 'trading212')",
    )
    date_from: datetime | None = Field(
        default=None,
        description="Earliest transaction date (default 90 days ago)",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Latest transaction date (default now)",
    )
    threshold_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        description="Max hour gap for duplicate candidates",
    )


class CompareConnectorsResponse(BaseModel):
    """Response from a connector-to-connector comparison."""

    connector_a: str
    connector_b: str
    run: ReconciliationRunResponse
    message: str = ""


class ReconciliationSummary(BaseModel):
    """Summary stats from a reconciliation run."""

    by_kind: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)


class ReconciliationRunResponse(BaseModel):
    """Public representation of a reconciliation run."""

    id: str
    tenant_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    scope: dict[str, Any] | None = None
    finding_count: int | None = None
    summary: ReconciliationSummary | None = None
    error_message: str | None = None
    created_at: datetime | None = None


class ReconciliationResultResponse(BaseModel):
    """Public representation of a single reconciliation finding."""

    id: str
    run_id: str
    kind: str
    severity: str
    account_id: str | None = None
    provider_key: str | None = None
    other_provider_key: str | None = None
    transaction_id_a: str | None = None
    transaction_id_b: str | None = None
    external_transaction_id_a: str | None = None
    external_transaction_id_b: str | None = None
    amount: Decimal | None = None
    other_amount: Decimal | None = None
    occurred_at: datetime | None = None
    description: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime | None = None
    transactions: list[TransactionReviewResponse] = Field(default_factory=list)


class TransactionReviewResponse(BaseModel):
    """The source and canonical fields needed to assess a duplicate."""

    id: str
    external_transaction_id: str
    provider_key: str
    account_id: str
    account_name: str | None = None
    security_id: str | None = None
    security_name: str | None = None
    occurred_at: datetime
    booked_at: datetime | None = None
    amount: Decimal
    currency_code: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    fee_amount: Decimal | None = None
    fee_currency_code: str | None = None
    transaction_type: str
    description: str | None = None
    status: str
    tombstoned_at: datetime | None = None
    provider_metadata_contract: dict[str, Any] | None = None


class DuplicateDecisionRequest(BaseModel):
    transaction_id_a: str
    transaction_id_b: str
    decision: Literal["keep_a", "keep_b", "keep_both"]
    finding_id: str | None = None


class ReconciliationRunListResponse(BaseModel):
    """List of reconciliation runs."""

    items: list[ReconciliationRunResponse]
    total: int
    limit: int
    offset: int


class ReconciliationRunDetailResponse(BaseModel):
    """A reconciliation run with its findings."""

    run: ReconciliationRunResponse
    results: list[ReconciliationResultResponse]
    total_results: int
    result_limit: int
    result_offset: int


ReconciliationRunDetailResponse.model_rebuild()
ReconciliationRunListResponse.model_rebuild()
ReconciliationRunResponse.model_rebuild()
ReconciliationResultResponse.model_rebuild()


# ── Helpers ───────────────────────────────────────────────────────────


def _run_to_response(run: object) -> ReconciliationRunResponse:
    """Convert an ORM ReconciliationRun to its response DTO."""
    summary_raw = getattr(run, "summary", None)
    summary_data: dict[str, Any] = (
        cast(dict[str, Any], summary_raw)
        if isinstance(summary_raw, dict)
        else {}
    )
    summary = ReconciliationSummary(
        by_kind=summary_data.get("by_kind", {}),
        by_severity=summary_data.get("by_severity", {}),
    )
    return ReconciliationRunResponse(
        id=str(getattr(run, "id", "")),
        tenant_id=str(getattr(run, "tenant_id", "")),
        status=str(getattr(run, "status", "")),
        started_at=getattr(run, "started_at", datetime.now(UTC)),
        completed_at=getattr(run, "completed_at", None),
        scope=getattr(run, "scope", None),
        finding_count=getattr(run, "finding_count", None),
        summary=summary,
        error_message=getattr(run, "error_message", None),
        created_at=getattr(run, "created_at", None),
    )


def _result_to_response(result: object) -> ReconciliationResultResponse:
    """Convert an ORM ReconciliationResult to its response DTO."""
    return ReconciliationResultResponse(
        id=str(getattr(result, "id", "")),
        run_id=str(getattr(result, "run_id", "")),
        kind=str(getattr(result, "kind", "")),
        severity=str(getattr(result, "severity", "")),
        account_id=str(getattr(result, "account_id", ""))
        if getattr(result, "account_id", None)
        else None,
        provider_key=getattr(result, "provider_key", None),
        other_provider_key=getattr(result, "other_provider_key", None),
        transaction_id_a=str(getattr(result, "transaction_id_a", ""))
        if getattr(result, "transaction_id_a", None)
        else None,
        transaction_id_b=str(getattr(result, "transaction_id_b", ""))
        if getattr(result, "transaction_id_b", None)
        else None,
        external_transaction_id_a=getattr(
            result, "external_transaction_id_a", None
        ),
        external_transaction_id_b=getattr(
            result, "external_transaction_id_b", None
        ),
        amount=getattr(result, "amount", None),
        other_amount=getattr(result, "other_amount", None),
        occurred_at=getattr(result, "occurred_at", None),
        description=getattr(result, "description", None),
        details=getattr(result, "details", None),
        created_at=getattr(result, "created_at", None),
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_reconciliation(
    body: ReconciliationTriggerRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
) -> dict[str, Any]:
    """Trigger a new reconciliation run.

    The run executes synchronously and returns the completed run with
    finding count and summary stats.
    """
    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    run = await svc.reconcile(
        account_ids=body.account_ids,
        date_from=body.date_from,
        date_to=body.date_to,
        threshold_hours=body.threshold_hours,
    )

    return _run_to_response(run).model_dump()


@router.post(
    "/compare",
    response_model=CompareConnectorsResponse,
    status_code=status.HTTP_200_OK,
)
async def compare_connectors(
    body: CompareConnectorsRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
) -> dict[str, Any]:
    """Compare two specific connectors/providers and return findings.

    Runs a reconciliation analysis limited to transactions from the two
    specified connector/provider keys.  Both connector keys are required
    and must be non-empty strings.  Returns the completed run with
    finding count, summary stats, and the compared connector IDs.
    """
    if body.connector_a == body.connector_b:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Connector IDs must be different, got "
                f"'{body.connector_a}' for both"
            ),
        )

    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    run = await svc.reconcile(
        provider_keys=[body.connector_a, body.connector_b],
        date_from=body.date_from,
        date_to=body.date_to,
        threshold_hours=body.threshold_hours,
    )

    if run.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Reconciliation failed: {run.error_message or 'Unknown error'}"
            ),
        )

    return CompareConnectorsResponse(
        connector_a=body.connector_a,
        connector_b=body.connector_b,
        run=_run_to_response(run),
        message=f"Compared '{body.connector_a}' vs '{body.connector_b}'",
    ).model_dump()


@router.post(
    "/trigger",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_reconciliation_v2(
    body: TriggerReconciliationRequest,
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "write")),
) -> dict[str, Any]:
    """Trigger a reconciliation run with optional connector comparison.

    Accepts optional ``connector_a`` and ``connector_b`` to limit the
    analysis to a specific pair of providers.  Also accepts scanning
    options such as ``detect_duplicates``.

    When ``connector_a`` and ``connector_b`` are both provided, the
    reconciliation is scoped to those two connectors.  When omitted,
    a full reconciliation across all connectors is performed.
    """
    provider_keys: list[str] | None = None
    if body.connector_a and body.connector_b:
        if body.connector_a == body.connector_b:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Connector IDs must be different, got "
                    f"'{body.connector_a}' for both"
                ),
            )
        provider_keys = [body.connector_a, body.connector_b]
    elif body.connector_a or body.connector_b:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Both connector_a and connector_b must be provided "
                "when specifying a connector comparison"
            ),
        )

    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    run = await svc.reconcile(
        account_ids=body.account_ids,
        provider_keys=provider_keys,
        date_from=body.date_from,
        date_to=body.date_to,
        threshold_hours=body.threshold_hours,
        detect_duplicates=body.detect_duplicates,
    )

    return _run_to_response(run).model_dump()


@router.get("", response_model=ReconciliationRunListResponse)
async def list_reconciliation_runs(
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List reconciliation runs for the tenant."""
    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    runs = await svc.list_runs(limit=limit, offset=offset)
    return {
        "items": [_run_to_response(r).model_dump() for r in runs],
        "total": len(runs),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{run_id}", response_model=ReconciliationRunDetailResponse)
async def get_reconciliation_run(
    run_id: str,
    request: Request,
    auth: AuthContext = Depends(require_permission("reconciliation", "read")),
    scope: ReadScope = Depends(get_read_scope),
    result_limit: int = Query(default=100, ge=1, le=500),
    result_offset: int = Query(default=0, ge=0),
    kind: str | None = Query(default=None),
    severity: str | None = Query(default=None),
) -> dict[str, Any]:
    """Get a reconciliation run with its findings.

    Findings referencing accounts outside the principal's account
    scope are hidden.
    """
    container = get_container(request)
    svc = ReconciliationService(
        session_factory=container.session_factory,
        tenant_id=auth.tenant_id,
    )

    run, results, total = await svc.get_run_with_results(
        run_id,
        result_limit=result_limit,
        result_offset=result_offset,
        kind_filter=kind,
        severity_filter=severity,
        visible_account_ids=scope.account_ids_subquery(),
    )

    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reconciliation run {run_id!r} not found",
        )

    if str(run.tenant_id) != auth.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
        )

    transaction_ids = {
        transaction_id
        for result in results
        for transaction_id in (
            getattr(result, "transaction_id_a", None),
            getattr(result, "transaction_id_b", None),
        )
        if transaction_id
    }
    transaction_details: dict[str, TransactionReviewResponse] = {}
    if transaction_ids:
        async with container.session_factory() as db:
            rows = await db.execute(
                select(Transaction, Account.name, Security.name)
                .join(Account, Account.id == Transaction.account_id)
                .outerjoin(Security, Security.id == Transaction.security_id)
                .where(
                    Transaction.tenant_id == auth.tenant_id,
                    Transaction.id.in_(transaction_ids),
                    Transaction.account_id.in_(scope.account_ids_subquery()),
                )
            )
            for tx, account_name, security_name in rows:
                transaction_details[str(tx.id)] = TransactionReviewResponse(
                    id=str(tx.id),
                    external_transaction_id=tx.external_transaction_id,
                    provider_key=tx.provider_key,
                    account_id=str(tx.account_id),
                    account_name=account_name,
                    security_id=str(tx.security_id) if tx.security_id else None,
                    security_name=security_name,
                    occurred_at=tx.occurred_at,
                    booked_at=tx.booked_at,
                    amount=tx.amount,
                    currency_code=tx.currency_code,
                    quantity=tx.quantity,
                    unit_price=tx.unit_price,
                    fee_amount=tx.fee_amount,
                    fee_currency_code=tx.fee_currency_code,
                    transaction_type=str(tx.transaction_type),
                    description=tx.description,
                    status=str(tx.status),
                    tombstoned_at=tx.tombstoned_at,
                    provider_metadata_contract=tx.provider_metadata_contract,
                )

    def result_response(result: object) -> dict[str, Any]:
        payload = _result_to_response(result).model_dump()
        payload["transactions"] = [
            transaction_details[transaction_id].model_dump()
            for transaction_id in (
                getattr(result, "transaction_id_a", None),
                getattr(result, "transaction_id_b", None),
            )
            if transaction_id and str(transaction_id) in transaction_details
        ]
        return payload

    return {
        "run": _run_to_response(run).model_dump(),
        "results": [result_response(r) for r in results],
        "total_results": total,
        "result_limit": result_limit,
        "result_offset": result_offset,
    }


@router.post("/duplicates/decision", response_model=dict[str, Any])
async def decide_duplicate(
    body: DuplicateDecisionRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record a duplicate decision and optionally tombstone one transaction."""
    if body.transaction_id_a == body.transaction_id_b:
        raise HTTPException(status_code=400, detail="Transactions must differ")
    ids = {body.transaction_id_a, body.transaction_id_b}
    rows = list(
        (
            await db.execute(
                select(Transaction).where(
                    Transaction.tenant_id == auth.tenant_id,
                    Transaction.id.in_(ids),
                )
            )
        ).scalars()
    )
    if len(rows) != 2:
        raise HTTPException(status_code=404, detail="Transaction not found")

    pair_key = ":".join(sorted(ids))
    kept_id = (
        body.transaction_id_a if body.decision == "keep_a" else
        body.transaction_id_b if body.decision == "keep_b" else None
    )
    review = await db.scalar(
        select(DuplicateReview).where(
            DuplicateReview.tenant_id == auth.tenant_id,
            DuplicateReview.pair_key == pair_key,
        )
    )
    if review is None:
        review = DuplicateReview(
            tenant_id=auth.tenant_id,
            transaction_id_a=body.transaction_id_a,
            transaction_id_b=body.transaction_id_b,
            pair_key=pair_key,
            decision=body.decision,
            kept_transaction_id=kept_id,
            actor=str(auth.user.id) if auth.user else None,
        )
        db.add(review)
    else:
        review.decision = body.decision
        review.kept_transaction_id = kept_id

    actor = str(auth.user.id) if auth.user else None
    for transaction in rows:
        db.add(
            TransactionLifecycleEvent(
                tenant_id=auth.tenant_id,
                transaction_id=transaction.id,
                event_type="duplicate_review",
                idempotency_key=(
                    f"duplicate-review:{pair_key}:{body.decision}:"
                    f"{transaction.id}:{uuid4()}"
                ),
                payload={
                    "decision": body.decision,
                    "pair_key": pair_key,
                    "finding_id": body.finding_id,
                },
                actor=actor,
                provenance="user_override",
                source_revision=transaction.revision,
            )
        )
    if kept_id is not None:
        async with UnitOfWork(db) as uow:
            await TransactionPersistence(auth.tenant_id).tombstone_transaction(
                uow,
                body.transaction_id_b if kept_id == body.transaction_id_a else body.transaction_id_a,
                actor=actor,
            )
    await db.commit()
    return {
        "pair_key": pair_key,
        "decision": body.decision,
        "kept_transaction_id": kept_id,
        "status": "reviewed",
    }
