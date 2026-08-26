"""Idempotent, transaction-friendly connector promotion and rollback flow."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

from sqlalchemy import select

from finance_sync.models import ConnectorRelease

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_ACTIVE = "enabled"


class ConnectorReleaseError(ValueError):
    """A candidate failed a release gate or requested transition."""


def _fail(code: str) -> NoReturn:
    raise ConnectorReleaseError(code)


def _validate_version(version: str) -> None:
    if not _SEMVER.fullmatch(version):
        _fail("invalid_version")


async def register_candidate(
    session: Any,
    *,
    provider_key: str,
    version: str,
    previous_version: str | None,
    certification_status: str,
    certification_commit: str | None,
    compatibility_status: str,
    canary_status: str,
    capabilities: list[str],
) -> ConnectorRelease:
    """Register or return a candidate; never enables it implicitly."""
    _validate_version(version)
    if certification_status not in {"pending", "certified"}:
        _fail("invalid_certification_status")
    stmt = select(ConnectorRelease).where(
        ConnectorRelease.provider_key == provider_key,
        ConnectorRelease.version == version,
    )
    release = (await session.execute(stmt)).scalar_one_or_none()
    if release is None:
        release = ConnectorRelease(
            provider_key=provider_key,
            version=version,
            status="certified"
            if certification_status == "certified"
            else "candidate",
            previous_version=previous_version,
            certification_status=certification_status,
            certification_commit=certification_commit,
            compatibility_status=compatibility_status,
            canary_status=canary_status,
            capabilities=sorted(set(capabilities)),
        )
        session.add(release)
    elif release.status in {"enabled", "rolled_back"}:
        return release
    else:
        release.previous_version = previous_version
        release.certification_status = certification_status
        release.certification_commit = certification_commit
        release.compatibility_status = compatibility_status
        release.canary_status = canary_status
        release.capabilities = sorted(set(capabilities))
        release.status = (
            "certified" if certification_status == "certified" else "candidate"
        )
    await session.flush()
    return release


async def promote(
    session: Any, provider_key: str, version: str
) -> ConnectorRelease:
    """Atomically enable a certified compatible candidate."""
    release = await _get(session, provider_key, version)
    if release.status == _ACTIVE:
        return release
    if release.certification_status != "certified":
        _fail("certification_required")
    if release.compatibility_status != "compatible":
        _fail("compatibility_required")
    if release.canary_status != "passed":
        _fail("canary_required")
    if release.status not in {"certified", "candidate", "blocked"}:
        _fail("release_not_promotable")
    current = await _enabled(session, provider_key)
    now = datetime.now(UTC)
    if current is not None and current.id != release.id:
        current.status = "deprecated"
        current.disabled_at = now
        release.previous_version = release.previous_version or current.version
    release.status = _ACTIVE
    release.reason_code = None
    release.enabled_at = release.enabled_at or now
    release.disabled_at = None
    await session.flush()
    return release


async def pause(
    session: Any, provider_key: str, version: str
) -> ConnectorRelease:
    release = await _get(session, provider_key, version)
    if release.status == "blocked":
        return release
    if release.status != _ACTIVE:
        _fail("release_not_enabled")
    release.status = "blocked"
    release.reason_code = "operator_paused"
    release.disabled_at = datetime.now(UTC)
    await session.flush()
    return release


async def resume(
    session: Any, provider_key: str, version: str
) -> ConnectorRelease:
    release = await _get(session, provider_key, version)
    if release.status == _ACTIVE:
        return release
    if (
        release.status != "blocked"
        or release.certification_status != "certified"
    ):
        _fail("release_not_resumable")
    return await promote(session, provider_key, version)


async def rollback(session: Any, provider_key: str) -> ConnectorRelease:
    current = await _enabled(session, provider_key)
    if current is None or not current.previous_version:
        _fail("previous_version_unavailable")
    previous_version = str(current.previous_version)
    previous = await _get(session, provider_key, previous_version)
    if previous.certification_status != "certified":
        _fail("previous_version_not_certified")
    now = datetime.now(UTC)
    current.status = "rolled_back"
    current.reason_code = "operator_rollback"
    current.disabled_at = now
    previous.status = _ACTIVE
    previous.enabled_at = previous.enabled_at or now
    previous.disabled_at = None
    await session.flush()
    return previous


async def _get(
    session: Any, provider_key: str, version: str
) -> ConnectorRelease:
    release = (
        await session.execute(
            select(ConnectorRelease).where(
                ConnectorRelease.provider_key == provider_key,
                ConnectorRelease.version == version,
            )
        )
    ).scalar_one_or_none()
    if release is None:
        _fail("release_not_found")
    return release


async def _enabled(session: Any, provider_key: str) -> ConnectorRelease | None:
    return cast(
        "ConnectorRelease | None",
        (
            await session.execute(
                select(ConnectorRelease).where(
                    ConnectorRelease.provider_key == provider_key,
                    ConnectorRelease.status == _ACTIVE,
                )
            )
        ).scalar_one_or_none(),
    )
