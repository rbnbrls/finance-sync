"""Privacy-first GlitchTip integration.

GlitchTip accepts the open-source Sentry SDK protocol.  This module keeps the
integration deliberately narrow: exceptions, sampled request/job traces and
safe diagnostic context.  Financial payloads and request data never leave the
process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import sentry_sdk

from finance_sync.config.settings import Settings, secret_value
from finance_sync.utils.redaction import redact_text

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

_SENSITIVE_KEYS = {
    "account",
    "account_id",
    "account_number",
    "access_token",
    "api_key",
    "authorization",
    "body",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "data",
    "description",
    "email",
    "financial",
    "iban",
    "password",
    "payload",
    "query_string",
    "raw_payload",
    "receipt",
    "secret",
    "token",
    "transaction_id",
    "user",
}
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: Sentry/GlitchTip protocol identifier fields.  These are opaque UUID/hex
#: values that the ingest endpoint *requires* to remain valid UUIDs — running
#: them through :func:`~finance_sync.utils.redaction.redact_text` turns them
#: into ``[REDACTED]`` (the 32+ char hex run matches ``_LONG_RUN_RE``), which
#: makes GlitchTip reject the whole envelope with HTTP 400.  They carry no
#: secret value (they are random IDs, not credentials), so they are exempted
#: from value redaction.
_PROTOCOL_ID_KEYS = frozenset(
    {
        "event_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "public_key",
        "profile_id",
        "replay_id",
        "check_in_id",
        "monitor_id",
        "cron_id",
    }
)


def _safe_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return normalized not in _SENSITIVE_KEYS and not any(
        part in normalized
        for part in ("password", "secret", "token", "credential", "iban")
    )


def _safe_value(value: Any, *, depth: int = 0, key: str | None = None) -> Any:
    """Copy event data while dropping sensitive fields and bounding strings.

    Protocol identifier values (``event_id``, ``trace_id``, ...) are returned
    verbatim: they must stay valid UUIDs for the ingest endpoint and are not
    secrets.
    """
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, Any]", value)
        return {
            str(key): _safe_value(item, depth=depth + 1, key=str(key))
            for key, item in mapping.items()
            if _safe_key(key)
        }
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return [
            _safe_value(item, depth=depth + 1, key=key) for item in items[:20]
        ]
    if isinstance(value, tuple):
        items = cast("tuple[Any, ...]", value)
        return [
            _safe_value(item, depth=depth + 1, key=key) for item in items[:20]
        ]
    if isinstance(value, str):
        if key is not None and key.casefold() in _PROTOCOL_ID_KEYS:
            return value[:500]
        return redact_text(value)[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if key is not None and key.casefold() in _PROTOCOL_ID_KEYS:
        return str(value)[:500]
    return redact_text(str(value))[:500]


def _safe_path(url: object) -> str | None:
    """Keep route diagnosis while removing host, query and identifiers."""
    if not isinstance(url, str):
        return None
    path = urlsplit(url).path or "/"
    segments = [
        ":id" if _UUID_SEGMENT.fullmatch(segment) else segment
        for segment in path.split("/")
    ]
    return "/".join(segments)[:300]


def scrub_event(event: Event, hint: Hint) -> Event:
    """Remove request/financial data before an event is sent to GlitchTip."""
    del hint
    event_dict = cast("dict[str, Any]", event)
    original_request = event_dict.get("request")
    original_url = (
        cast("Mapping[str, Any]", original_request).get("url")
        if isinstance(original_request, Mapping)
        else None
    )
    safe = cast("dict[str, Any]", _safe_value(event_dict))

    request = safe.get("request")
    if isinstance(request, dict):
        request = cast("dict[str, Any]", request)
        request_url = original_url
        request_method = request.get("method", "")
        request.clear()
        if request_url:
            request["url"] = _safe_path(request_url)
        request["method"] = request_method

    # Breadcrumb data frequently contains HTTP client arguments or payloads.
    breadcrumbs = safe.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        breadcrumbs = cast("dict[str, Any]", breadcrumbs)
        values = breadcrumbs.get("values")
        if isinstance(values, list):
            values = cast("list[Any]", values)
            for breadcrumb in values:
                if isinstance(breadcrumb, dict):
                    breadcrumb = cast("dict[str, Any]", breadcrumb)
                    breadcrumb.pop("data", None)

    safe.pop("user", None)
    safe.pop("extra", None)
    return cast("Event", safe)


def configure_glitchtip(settings: Settings) -> bool:
    """Configure GlitchTip once for the current process.

    Returns ``True`` only when an SDK client was configured.  A missing DSN or
    disabled flag is intentionally a no-op so local tests and development do
    not contact an external service.
    """
    dsn = secret_value(settings.glitchtip_dsn)
    if not settings.glitchtip_enabled or not dsn:
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.environment.value,
        release=settings.glitchtip_release or settings.app_version,
        sample_rate=settings.glitchtip_sample_rate,
        traces_sample_rate=settings.glitchtip_traces_sample_rate,
        max_breadcrumbs=settings.glitchtip_max_breadcrumbs,
        send_default_pii=False,
        include_local_variables=False,
        before_send=scrub_event,
    )
    return True


def capture_job_exception(error: BaseException) -> None:
    """Capture a worker exception without exposing its raw context."""
    sentry_sdk.capture_exception(error)


def _correlation_value(value: str | None) -> str | None:
    """Return a stable, non-reversible identifier for error correlation."""
    if not value:
        return None
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def capture_sync_exception(
    error: BaseException,
    *,
    connector: str,
    operation: str,
    connection_id: str | None = None,
    sync_run_id: str | None = None,
    account_id: str | None = None,
) -> None:
    """Capture a sync failure with safe operation/correlation metadata.

    Identifiers are hashed so the GlitchTip → GitHub workflow can correlate
    repeated failures without exposing account, tenant, or credential data.
    Event values remain subject to :func:`scrub_event` before transport.
    """
    with sentry_sdk.push_scope() as scope:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        safe_scope: Any = cast("Any", scope)
        safe_scope.set_tag("connector", connector)  # pyright: ignore[reportUnknownMemberType]
        safe_scope.set_tag("sync_operation", operation)  # pyright: ignore[reportUnknownMemberType]
        for key, value in (
            ("connection_id", connection_id),
            ("sync_run_id", sync_run_id),
            ("account_id", account_id),
        ):
            correlation = _correlation_value(value)
            if correlation is not None:
                safe_scope.set_tag(key, correlation)  # pyright: ignore[reportUnknownMemberType]
        sentry_sdk.capture_exception(error)


def capture_connector_exception(
    error: BaseException,
    *,
    connector: str,
    operation: str,
    connection_id: str | None = None,
    provider_account_id: str | None = None,
    correlation_id: str | None = None,
    fingerprint: str | None = None,
) -> None:
    """Capture a connector failure with safe diagnostic context."""
    with sentry_sdk.push_scope() as scope:
        safe_scope: Any = cast("Any", scope)
        safe_scope.set_tag("connector", connector)
        safe_scope.set_tag("operation", operation)
        if fingerprint:
            safe_scope.set_tag("incident_fingerprint", fingerprint)
            safe_scope.set_fingerprint(["finance-sync-incident", fingerprint])
        if correlation_id:
            safe_scope.set_tag("correlation_id", correlation_id)
        context = {"connector": connector, "operation": operation}
        for key, value in (
            ("connection_fingerprint", connection_id),
            ("provider_account_fingerprint", provider_account_id),
        ):
            fingerprint = _correlation_value(value)
            if fingerprint:
                context[key] = fingerprint
        safe_scope.set_context("connector", context)
        sentry_sdk.capture_exception(error)
