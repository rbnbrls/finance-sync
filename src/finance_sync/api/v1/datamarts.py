"""Administrative API for governed downstream datamarts."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_sync.api.deps.auth import (
    AuthContext,
    get_auth_context,
    require_role,
)
from finance_sync.dependencies import get_db
from finance_sync.models import (
    Account,
    ApiKey,
    DataMart,
    DataMartConsumer,
    DataMartGrant,
)
from finance_sync.models.datamart import DELIVERY_METHODS, HOUSEHOLD_SCOPES
from finance_sync.services.datamart_policy import effective_grant

router = APIRouter(prefix="/datamarts", tags=["datamarts"])

_Admin = Annotated[AuthContext, Depends(require_role("admin"))]


class DataMartCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=128)
    dataset: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    schema_version: str = Field(pattern=r"^pfc/\d+\.\d+$")
    fields: list[str] = Field(min_length=1)
    delivery_method: str
    delivery_config: dict[str, object] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def _unique_fields(cls, value: list[str]) -> list[str]:
        if any(not field or len(field) > 128 for field in value):
            msg = "fields must be non-empty and at most 128 characters"
            raise ValueError(msg)
        if len(set(value)) != len(value):
            msg = "fields must be unique"
            raise ValueError(msg)
        return value

    @field_validator("delivery_method")
    @classmethod
    def _delivery_method(cls, value: str) -> str:
        if value not in DELIVERY_METHODS:
            msg = f"delivery_method must be one of {sorted(DELIVERY_METHODS)}"
            raise ValueError(msg)
        return value


class DataMartResponse(BaseModel):
    id: str
    key: str
    display_name: str
    dataset: str
    schema_version: str
    fields: list[str]
    delivery_method: str
    delivery_config: dict[str, object]
    is_active: bool


class ConsumerCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=128)
    api_key_id: str | None = None


class ConsumerResponse(BaseModel):
    id: str
    key: str
    display_name: str
    api_key_id: str | None
    is_active: bool


class GrantCreate(BaseModel):
    consumer_id: str
    datamart_id: str
    household_scope: str = "explicit"
    allowed_account_ids: list[str] = Field(default_factory=list)
    allowed_fields: list[str] = Field(default_factory=list)

    @field_validator("household_scope")
    @classmethod
    def _household_scope(cls, value: str) -> str:
        if value not in HOUSEHOLD_SCOPES:
            msg = f"household_scope must be one of {sorted(HOUSEHOLD_SCOPES)}"
            raise ValueError(msg)
        return value

    @field_validator("allowed_account_ids", "allowed_fields")
    @classmethod
    def _unique_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            msg = "values must be unique"
            raise ValueError(msg)
        return value


class GrantResponse(BaseModel):
    id: str
    consumer_id: str
    datamart_id: str
    household_scope: str
    allowed_account_ids: list[str]
    allowed_fields: list[str]
    is_active: bool


class EffectiveGrantResponse(BaseModel):
    datamart_key: str
    dataset: str
    schema_version: str
    fields: list[str]
    delivery_method: str
    household_scope: str
    allowed_account_ids: list[str]


class ConsumerPolicyResponse(BaseModel):
    consumer_id: str
    consumer_key: str
    grants: list[EffectiveGrantResponse]


def _mart_response(mart: DataMart) -> DataMartResponse:
    return DataMartResponse(
        id=str(mart.id),
        key=mart.key,
        display_name=mart.display_name,
        dataset=mart.dataset,
        schema_version=mart.schema_version,
        fields=list(mart.fields or []),
        delivery_method=mart.delivery_method,
        delivery_config=dict(mart.delivery_config or {}),
        is_active=mart.is_active,
    )


def _consumer_response(consumer: DataMartConsumer) -> ConsumerResponse:
    return ConsumerResponse(
        id=str(consumer.id),
        key=consumer.key,
        display_name=consumer.display_name,
        api_key_id=str(consumer.api_key_id) if consumer.api_key_id else None,
        is_active=consumer.is_active,
    )


@router.post(
    "", response_model=DataMartResponse, status_code=status.HTTP_201_CREATED
)
async def create_datamart(
    body: DataMartCreate, auth: _Admin, db: AsyncSession = Depends(get_db)
) -> DataMartResponse:
    existing = await db.scalar(
        select(DataMart.id).where(
            DataMart.tenant_id == auth.tenant_id, DataMart.key == body.key
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="Datamart key already exists"
        )
    mart = DataMart(tenant_id=auth.tenant_id, **body.model_dump())
    db.add(mart)
    await db.flush()
    return _mart_response(mart)


@router.get("", response_model=list[DataMartResponse])
async def list_datamarts(
    auth: _Admin, db: AsyncSession = Depends(get_db)
) -> list[DataMartResponse]:
    rows = (
        (
            await db.execute(
                select(DataMart)
                .where(DataMart.tenant_id == auth.tenant_id)
                .order_by(DataMart.key)
            )
        )
        .scalars()
        .all()
    )
    return [_mart_response(row) for row in rows]


@router.post(
    "/consumers",
    response_model=ConsumerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_consumer(
    body: ConsumerCreate, auth: _Admin, db: AsyncSession = Depends(get_db)
) -> ConsumerResponse:
    if await db.scalar(
        select(DataMartConsumer.id).where(
            DataMartConsumer.tenant_id == auth.tenant_id,
            DataMartConsumer.key == body.key,
        )
    ):
        raise HTTPException(
            status_code=409, detail="Consumer key already exists"
        )
    if (
        body.api_key_id
        and await db.scalar(
            select(ApiKey.id).where(
                ApiKey.id == body.api_key_id, ApiKey.tenant_id == auth.tenant_id
            )
        )
        is None
    ):
        raise HTTPException(
            status_code=422, detail="API key does not belong to this tenant"
        )
    consumer = DataMartConsumer(tenant_id=auth.tenant_id, **body.model_dump())
    db.add(consumer)
    await db.flush()
    return _consumer_response(consumer)


@router.post(
    "/grants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED
)
async def create_grant(
    body: GrantCreate, auth: _Admin, db: AsyncSession = Depends(get_db)
) -> GrantResponse:
    consumer = await db.scalar(
        select(DataMartConsumer).where(
            DataMartConsumer.id == body.consumer_id,
            DataMartConsumer.tenant_id == auth.tenant_id,
        )
    )
    mart = await db.scalar(
        select(DataMart).where(
            DataMart.id == body.datamart_id,
            DataMart.tenant_id == auth.tenant_id,
        )
    )
    if consumer is None or mart is None:
        raise HTTPException(
            status_code=404, detail="Consumer or datamart not found"
        )
    if await db.scalar(
        select(DataMartGrant.id).where(
            DataMartGrant.consumer_id == consumer.id,
            DataMartGrant.datamart_id == mart.id,
        )
    ):
        raise HTTPException(status_code=409, detail="Grant already exists")
    if set(body.allowed_fields) - set(mart.fields or []):
        raise HTTPException(
            status_code=422,
            detail="Grant fields must be a subset of datamart fields",
        )
    if body.allowed_account_ids:
        count = await db.scalar(
            select(func.count())
            .select_from(Account)
            .where(
                Account.tenant_id == auth.tenant_id,
                Account.id.in_(body.allowed_account_ids),
            )
        )
        if count != len(body.allowed_account_ids):
            raise HTTPException(
                status_code=422,
                detail="An allowed account does not belong to this tenant",
            )
    grant = DataMartGrant(tenant_id=auth.tenant_id, **body.model_dump())
    db.add(grant)
    await db.flush()
    return GrantResponse(
        id=str(grant.id), **body.model_dump(), is_active=grant.is_active
    )


@router.get(
    "/consumers/{consumer_id}/policy", response_model=ConsumerPolicyResponse
)
async def get_consumer_policy(
    consumer_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> ConsumerPolicyResponse:
    consumer = await db.scalar(
        select(DataMartConsumer).where(
            DataMartConsumer.id == consumer_id,
            DataMartConsumer.tenant_id == auth.tenant_id,
            DataMartConsumer.is_active,
        )
    )
    if consumer is None:
        raise HTTPException(status_code=404, detail="Consumer not found")
    is_admin = auth.user is not None and auth.user.role == "admin"
    is_bound_key = (
        auth.api_key_result is not None
        and auth.api_key_result.api_key is not None
        and str(auth.api_key_result.api_key.id) == str(consumer.api_key_id)
    )
    if not is_admin and not is_bound_key:
        raise HTTPException(
            status_code=403,
            detail="Consumer identity does not match this policy",
        )
    rows = (
        await db.execute(
            select(DataMartGrant, DataMart)
            .join(DataMart, DataMart.id == DataMartGrant.datamart_id)
            .where(
                DataMartGrant.consumer_id == consumer.id,
                DataMartGrant.tenant_id == auth.tenant_id,
                DataMartGrant.is_active,
                DataMart.is_active,
            )
        )
    ).all()
    grants: list[EffectiveGrantResponse] = []
    for grant, mart in rows:
        policy = effective_grant(
            cast(DataMart, mart), cast(DataMartGrant, grant)
        )
        grants.append(
            EffectiveGrantResponse(
                datamart_key=policy.datamart_key,
                dataset=policy.dataset,
                schema_version=policy.schema_version,
                fields=list(policy.fields),
                delivery_method=policy.delivery_method,
                household_scope=policy.household_scope,
                allowed_account_ids=list(policy.allowed_account_ids),
            )
        )
    return ConsumerPolicyResponse(
        consumer_id=str(consumer.id), consumer_key=consumer.key, grants=grants
    )
