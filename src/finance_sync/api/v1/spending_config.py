"""Tenant-scoped management API for merchant and category mappings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import AuthContext, require_permission
from finance_sync.dependencies import get_db
from finance_sync.models import (
    CategoryMapping,
    SpendingPrivacyPolicy,
    SpendingRule,
    StoredMerchantMapping,
)

router = APIRouter(prefix="/spending", tags=["spending"])


class MerchantMappingRequest(BaseModel):
    merchant_key: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    category: str | None = Field(default=None, max_length=256)
    taxonomy: str | None = Field(default=None, max_length=128)


class CategoryMappingRequest(BaseModel):
    taxonomy: str = Field(min_length=1, max_length=128)
    source_category: str = Field(min_length=1, max_length=256)
    destination_type: str = Field(min_length=1, max_length=64)
    destination_category: str = Field(min_length=1, max_length=256)


class SpendingRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    condition: dict[str, Any] = Field(default_factory=dict)
    actions: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=100000)
    enabled: bool = True


class SpendingPrivacyPolicyRequest(BaseModel):
    enabled_fields: list[str] = Field(default_factory=list)
    retention_days: int = Field(default=365, ge=1, le=3650)
    allow_attachments: bool = False
    allow_raw_payload: bool = False


def _mapping_response(row: Any) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "merchant_key",
            "display_name",
            "category",
            "taxonomy",
            "normalization_version",
            "source_category",
            "destination_type",
            "destination_category",
        )
        if getattr(row, key, None) is not None
    }


@router.get("/mappings")
async def list_spending_mappings(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    merchant_rows = (
        (
            await db.execute(
                select(StoredMerchantMapping).where(
                    StoredMerchantMapping.tenant_id == auth.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    category_rows = (
        (
            await db.execute(
                select(CategoryMapping).where(
                    CategoryMapping.tenant_id == auth.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "merchant_mappings": [_mapping_response(row) for row in merchant_rows],
        "category_mappings": [_mapping_response(row) for row in category_rows],
    }


@router.post("/merchant-mappings", status_code=status.HTTP_201_CREATED)
async def create_merchant_mapping(
    body: MerchantMappingRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = StoredMerchantMapping(tenant_id=auth.tenant_id, **body.model_dump())
    db.add(row)
    await db.commit()
    return _mapping_response(row)


@router.post("/category-mappings", status_code=status.HTTP_201_CREATED)
async def create_category_mapping(
    body: CategoryMappingRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = CategoryMapping(tenant_id=auth.tenant_id, **body.model_dump())
    db.add(row)
    await db.commit()
    return _mapping_response(row)


@router.get("/rules")
async def list_spending_rules(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(SpendingRule)
                .where(SpendingRule.tenant_id == auth.tenant_id)
                .order_by(SpendingRule.priority, SpendingRule.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "condition": row.condition,
            "actions": row.actions,
            "priority": row.priority,
            "enabled": row.enabled,
            "provenance": row.provenance,
        }
        for row in rows
    ]


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_spending_rule(
    body: SpendingRuleRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = SpendingRule(
        tenant_id=auth.tenant_id,
        name=body.name,
        condition=body.condition,
        actions=body.actions,
        priority=body.priority,
        enabled=body.enabled,
        provenance="user",
    )
    db.add(row)
    await db.commit()
    return {"id": str(row.id), "name": row.name}


@router.get("/privacy-policy")
async def get_spending_privacy_policy(
    auth: AuthContext = Depends(require_permission("transactions", "read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await db.scalar(
        select(SpendingPrivacyPolicy).where(
            SpendingPrivacyPolicy.tenant_id == auth.tenant_id
        )
    )
    if row is None:
        return {
            "enabled_fields": [],
            "retention_days": 365,
            "allow_attachments": False,
            "allow_raw_payload": False,
        }
    return {
        "id": str(row.id),
        "enabled_fields": row.enabled_fields,
        "retention_days": row.retention_days,
        "allow_attachments": row.allow_attachments,
        "allow_raw_payload": row.allow_raw_payload,
        "provenance": row.provenance,
    }


@router.put("/privacy-policy")
async def put_spending_privacy_policy(
    body: SpendingPrivacyPolicyRequest,
    auth: AuthContext = Depends(require_permission("transactions", "write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await db.scalar(
        select(SpendingPrivacyPolicy).where(
            SpendingPrivacyPolicy.tenant_id == auth.tenant_id
        )
    )
    if row is None:
        row = SpendingPrivacyPolicy(tenant_id=auth.tenant_id)
        db.add(row)
    row.enabled_fields = body.enabled_fields
    row.retention_days = body.retention_days
    row.allow_attachments = body.allow_attachments
    row.allow_raw_payload = body.allow_raw_payload
    row.provenance = "user"
    await db.commit()
    return await get_spending_privacy_policy(auth=auth, db=db)
