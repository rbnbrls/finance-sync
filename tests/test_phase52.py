"""Tests for Phase 5.2: AI summaries & Home Assistant integration.

# pyright: basic

Covers:
- Settings: new AI/HA fields parse correctly
- AI summary service: unit tests with mocked HTTP client
- HA integration service: unit tests with mocked session
- API endpoint registration
- Auth guards on new endpoints
- Feature toggle (disable via settings)
- AI rate limiting
- Cache behaviour
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

from finance_sync.app import create_app
from finance_sync.config.settings import Settings
from finance_sync.dependencies import get_db

# ── Test helpers ──────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-key-at-least-16-chars"
_API_KEY_TOKEN = "test-api-key-0123456789abcdef"


def _auth_header(token: str = _API_KEY_TOKEN) -> dict[str, str]:  # type: ignore[reportUnusedFunction]
    return {"Authorization": f"Bearer {token}"}


# ── Shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        ai_enabled=True,
        ha_enabled=True,
        ai_provider="openai",
        ai_api_key="sk-test-key",
        ai_model="gpt-4o",
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def settings_ai_disabled() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        ai_enabled=False,
        ha_enabled=True,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def settings_ha_disabled() -> Settings:
    return Settings(
        secret_key=_TEST_SECRET,
        ai_enabled=True,
        ha_enabled=False,
        database_url=None,
        redis_url=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings=settings)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture
def app_ai_disabled(settings_ai_disabled: Settings) -> FastAPI:
    app = create_app(settings=settings_ai_disabled)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture
def app_ha_disabled(settings_ha_disabled: Settings) -> FastAPI:
    app = create_app(settings=settings_ha_disabled)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_ai_disabled(
    app_ai_disabled: FastAPI,
) -> Generator[TestClient, None, None]:
    with TestClient(app_ai_disabled) as c:
        yield c


@pytest.fixture
def client_ha_disabled(
    app_ha_disabled: FastAPI,
) -> Generator[TestClient, None, None]:
    with TestClient(app_ha_disabled) as c:
        yield c


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalar.return_value = 0
    mock_result.scalar_one_or_none.return_value = None
    mock_result.all.return_value = []
    session.execute.return_value = mock_result
    return session


# ═══════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════


class TestAISettings:
    """AI-related settings parse correctly."""

    def test_ai_defaults(self) -> None:
        s = Settings(secret_key=_TEST_SECRET)
        assert s.ai_enabled is True
        assert s.ai_provider == "openai"
        assert s.ai_api_key is None
        assert s.ai_model == "gpt-4o"
        assert s.ai_summary_cache_ttl_seconds == 3600
        assert s.ai_rate_limit_max_requests == 20
        assert s.ai_rate_limit_window_seconds == 3600
        assert s.ai_summary_max_length == 500

    def test_ai_provider_custom(self) -> None:
        s = Settings(
            secret_key=_TEST_SECRET,
            ai_provider="anthropic",
            ai_api_key="sk-ant-test",
            ai_model="claude-sonnet-4",
        )
        assert s.ai_provider == "anthropic"
        assert s.ai_api_key is not None
        assert s.ai_api_key.get_secret_value() == "sk-ant-test"
        assert s.ai_model == "claude-sonnet-4"

    def test_ai_disabled(self) -> None:
        s = Settings(secret_key=_TEST_SECRET, ai_enabled=False)
        assert s.ai_enabled is False


class TestHASettings:
    """HA integration settings parse correctly."""

    def test_ha_defaults(self) -> None:
        s = Settings(secret_key=_TEST_SECRET)
        assert s.ha_enabled is True

    def test_ha_disabled(self) -> None:
        s = Settings(secret_key=_TEST_SECRET, ha_enabled=False)
        assert s.ha_enabled is False


# ═══════════════════════════════════════════════════════════════════════
# AI Summary Service
# ═══════════════════════════════════════════════════════════════════════


class TestAISummaryService:
    """AISummaryService unit tests with mocked HTTP client."""

    @pytest.fixture
    def svc(self, mock_session: AsyncMock, settings: Settings) -> Any:
        from finance_sync.services.ai_summary import AISummaryService

        return AISummaryService(mock_session, settings)

    async def test_summary_no_api_key_raises(
        self, mock_session: AsyncMock
    ) -> None:
        """Missing API key raises RuntimeError."""
        from finance_sync.services.ai_summary import AISummaryService

        s = Settings(secret_key=_TEST_SECRET, ai_api_key=None)
        svc = AISummaryService(mock_session, s)

        with pytest.raises(RuntimeError, match="no AI_API_KEY configured"):
            await svc.generate_summary(tenant_id="tenant-1")

    @patch("finance_sync.services.ai_summary.AISummaryService._call_openai")
    async def test_summary_calls_openai(
        self,
        mock_call: AsyncMock,
        svc: Any,
    ) -> None:
        """generate_summary calls the LLM and returns text."""
        mock_call.return_value = (
            "gpt-4o",
            "Your spending increased 5% this month.",
        )

        result = await svc.generate_summary(tenant_id="tenant-1")

        assert result.summary == "Your spending increased 5% this month."
        assert result.model == "gpt-4o"
        assert result.source == "ai_generated"
        assert result.generated_at is not None

    @patch("finance_sync.services.ai_summary.AISummaryService._call_openai")
    async def test_summary_cache(
        self,
        mock_call: AsyncMock,
        svc: Any,
    ) -> None:
        """Subsequent calls return cached result without calling the LLM."""
        from finance_sync.services.ai_summary import _CACHE

        # Clear cache first
        _CACHE.clear()
        mock_call.return_value = ("gpt-4o", "Summary text.")

        # First call - should call LLM
        r1 = await svc.generate_summary(tenant_id="tenant-1")
        assert mock_call.call_count == 1

        # Second call - should use cache
        r2 = await svc.generate_summary(tenant_id="tenant-1")
        assert mock_call.call_count == 1  # not called again
        assert r1.summary == r2.summary

    @patch("finance_sync.services.ai_summary.AISummaryService._call_openai")
    async def test_force_refresh_bypasses_cache(
        self,
        mock_call: AsyncMock,
        svc: Any,
    ) -> None:
        """force_refresh=True bypasses the cache."""
        from finance_sync.services.ai_summary import _CACHE

        _CACHE.clear()
        mock_call.return_value = ("gpt-4o", "Original summary.")

        await svc.generate_summary(tenant_id="tenant-1")
        assert mock_call.call_count == 1

        mock_call.return_value = ("gpt-4o", "Refreshed summary.")
        r2 = await svc.generate_summary(
            tenant_id="tenant-1", force_refresh=True
        )
        assert mock_call.call_count == 2
        assert "Refreshed" in r2.summary

    @patch("finance_sync.services.ai_summary.AISummaryService._call_openai")
    async def test_daily_briefing(
        self,
        mock_call: AsyncMock,
        svc: Any,
    ) -> None:
        """Daily briefing generates and returns text."""
        mock_call.return_value = (
            "gpt-4o",
            "Good morning! Your net worth increased by 2% today.",
        )

        result = await svc.generate_daily_briefing(tenant_id="tenant-1")

        assert "Good morning" in result.briefing
        assert result.model == "gpt-4o"
        assert result.date is not None

    @patch("finance_sync.services.ai_summary.AISummaryService._call_openai")
    async def test_daily_briefing_cache(
        self,
        mock_call: AsyncMock,
        svc: Any,
    ) -> None:
        """Daily briefing caches per-day."""
        from finance_sync.services.ai_summary import _CACHE

        _CACHE.clear()
        mock_call.return_value = ("gpt-4o", "Briefing text.")

        r1 = await svc.generate_daily_briefing(tenant_id="tenant-1")
        assert mock_call.call_count == 1

        r2 = await svc.generate_daily_briefing(tenant_id="tenant-1")
        assert mock_call.call_count == 1  # cached
        assert r1.briefing == r2.briefing

    async def test_openai_request_format(self, settings: Settings) -> None:
        """OpenAI API call builds correct request body."""
        from finance_sync.services.ai_summary import AISummaryService

        mock_sesh = AsyncMock()
        svc = AISummaryService(mock_sesh, settings)

        with patch.object(svc, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "Hello"}}],
                "model": "gpt-4o",
            }
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            await svc._call_openai("sk-test", "Test prompt")

            # Verify the request was built correctly
            call_kwargs = mock_client.post.call_args[1]
            assert call_kwargs["json"]["model"] == "gpt-4o"
            assert call_kwargs["json"]["messages"] == [
                {"role": "user", "content": "Test prompt"}
            ]
            assert call_kwargs["headers"]["Authorization"] == "Bearer sk-test"

    async def test_anthropic_request_format(self, settings: Settings) -> None:
        """Anthropic API call builds correct request body."""
        from finance_sync.services.ai_summary import AISummaryService

        s = Settings(
            secret_key=_TEST_SECRET,
            ai_provider="anthropic",
            ai_api_key="sk-ant-test",
            ai_model="claude-sonnet-4",
        )
        mock_sesh = AsyncMock()
        svc = AISummaryService(mock_sesh, s)

        with patch.object(svc, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "content": [{"type": "text", "text": "Hello from Claude"}],
                "model": "claude-sonnet-4",
            }
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            (model, text) = await svc._call_anthropic(
                "sk-ant-test", "Test prompt"
            )

            assert model == "claude-sonnet-4"
            assert text == "Hello from Claude"
            call_kwargs = mock_client.post.call_args[1]
            assert "x-api-key" in call_kwargs["headers"]
            assert call_kwargs["json"]["model"] == "claude-sonnet-4"

    async def test_unsupported_provider(self, mock_session: AsyncMock) -> None:
        """Unsupported provider raises ValueError."""
        from finance_sync.services.ai_summary import AISummaryService

        s = Settings(
            secret_key=_TEST_SECRET,
            ai_provider="ollama",
            ai_api_key="test",
        )
        svc = AISummaryService(mock_session, s)

        with pytest.raises(ValueError, match="Unsupported AI provider"):
            await svc._call_llm("test prompt")

    async def test_close_releases_client(
        self, mock_session: AsyncMock, settings: Settings
    ) -> None:
        """close() releases the HTTP client."""
        from finance_sync.services.ai_summary import AISummaryService

        svc = AISummaryService(mock_session, settings)
        # Init the client
        await svc._get_client()
        await svc.close()
        assert svc._http_client is None


# ═══════════════════════════════════════════════════════════════════════
# HA Integration Service
# ═══════════════════════════════════════════════════════════════════════


class TestHomeAssistantService:
    """HomeAssistantService unit tests."""

    @pytest.fixture
    def svc(self, mock_session: AsyncMock, settings: Settings) -> Any:
        from finance_sync.services.ha_integration import HomeAssistantService

        return HomeAssistantService(mock_session, settings)

    async def test_get_sensors_returns_all_five(
        self, svc: Any, mock_session: AsyncMock
    ) -> None:
        """get_sensors returns all 5 sensor types."""
        # Mock read_service data
        from finance_sync.services.read_api import (
            AccountDetailResponse,
            AccountSummary,
            NetWorthResponse,
            PortfolioResponse,
        )

        # Patch the read_service calls
        svc._read_service.get_net_worth = AsyncMock(
            return_value=NetWorthResponse(
                total_assets=Decimal(10000),
                total_liabilities=Decimal(2000),
                net_worth=Decimal(8000),
                as_of=datetime.now(UTC),
            )
        )
        svc._read_service.get_portfolio = AsyncMock(
            return_value=PortfolioResponse(
                accounts=[],
                total_value=Decimal(5000),
                total_cost_basis=Decimal(4500),
            )
        )
        svc._read_service.list_accounts = AsyncMock(
            return_value=AccountDetailResponse(
                items=[
                    AccountSummary(
                        id="1",
                        name="Checking",
                        account_type="checking",
                        currency_code="EUR",
                        provider_key="bunq",
                        is_active=True,
                    )
                ],
                total=1,
                limit=100,
                offset=0,
            )
        )

        sensors = await svc.get_sensors(tenant_id="tenant-1")

        assert len(sensors) == 5
        sensor_map = {s.sensor_id: s for s in sensors}

        assert sensor_map["sensor.finance_sync_net_worth"].value == "8000.00"
        assert (
            sensor_map["sensor.finance_sync_portfolio_value"].value == "5000.00"
        )
        assert sensor_map["sensor.finance_sync_account_count"].value == "1"
        assert sensor_map["sensor.finance_sync_last_sync"].value == "never"
        assert (
            sensor_map["sensor.finance_sync_sync_status"].value
            == "never_synced"
        )

    async def test_get_sensors_with_sync_data(
        self, svc: Any, mock_session: AsyncMock
    ) -> None:
        """get_sensors reflects sync run history."""
        from finance_sync.services.read_api import (
            AccountDetailResponse,
            NetWorthResponse,
            PortfolioResponse,
        )

        svc._read_service.get_net_worth = AsyncMock(
            return_value=NetWorthResponse(net_worth=Decimal(5000))
        )
        svc._read_service.get_portfolio = AsyncMock(
            return_value=PortfolioResponse(
                accounts=[],
                total_value=Decimal(3000),
            )
        )
        svc._read_service.list_accounts = AsyncMock(
            return_value=AccountDetailResponse(
                items=[], total=2, limit=100, offset=0
            )
        )

        # Mock sync run query — returns sync_run then count

        mock_run = MagicMock()
        mock_run.status = "completed"
        mock_run.connector = "bunq"
        mock_run.started_at = datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)
        type(mock_run).started_at = PropertyMock(
            return_value=datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)
        )

        # Configure execute to return sync_run for first call
        execute_results: list[Any] = []

        first_mock = MagicMock()
        first_mock.scalar_one_or_none.return_value = mock_run
        execute_results.append(first_mock)

        second_mock = MagicMock()
        second_mock.scalar.return_value = 0
        execute_results.append(second_mock)

        mock_session.execute = AsyncMock(side_effect=execute_results)

        sensors = await svc.get_sensors(tenant_id="tenant-1")
        sensor_map = {s.sensor_id: s for s in sensors}

        assert "2026-07-21" in sensor_map["sensor.finance_sync_last_sync"].value
        assert sensor_map["sensor.finance_sync_sync_status"].value == "healthy"

    def test_get_config(self, settings: Settings) -> None:
        """get_config returns the config structure."""
        from finance_sync.services.ha_integration import HomeAssistantService

        svc = HomeAssistantService(session=None, settings=settings)  # type: ignore[arg-type]
        result = svc.get_config(
            base_url="http://localhost:8000/api/v1/ha/sensors"
        )

        assert result.base_url == "http://localhost:8000/api/v1/ha/sensors"
        assert len(result.sensor_ids) == 5
        assert "sensor.finance_sync_net_worth" in result.sensor_ids

    def test_sensor_to_dict(self) -> None:
        """HASensor.to_dict returns HA REST sensor format."""
        from finance_sync.services.ha_integration import HASensor

        sensor = HASensor(
            sensor_id="sensor.finance_sync_net_worth",
            name="Finance Sync Net Worth",
            value="8000.00",
            unit_of_measurement="EUR",
            icon="mdi:currency-eur",
            state_class="measurement",
        )

        d = sensor.to_dict()
        assert d["state"] == "8000.00"
        assert d["attributes"]["friendly_name"] == "Finance Sync Net Worth"
        assert d["attributes"]["unit_of_measurement"] == "EUR"
        assert d["attributes"]["icon"] == "mdi:currency-eur"


# ═══════════════════════════════════════════════════════════════════════
# OpenAPI endpoint registration
# ═══════════════════════════════════════════════════════════════════════


class TestOpenAPIRegistration:
    """Verify Phase 5.2 endpoints appear in the OpenAPI schema."""

    def _paths(self, client: TestClient) -> dict[str, Any]:
        return client.get("/openapi.json").json()["paths"]

    def test_ai_summary_endpoints_registered(self, client: TestClient) -> None:
        paths = self._paths(client)
        assert "/api/v1/ai/summary" in paths
        assert "/api/v1/ai/summary/daily" in paths

    def test_ha_endpoints_registered(self, client: TestClient) -> None:
        paths = self._paths(client)
        assert "/api/v1/ha/sensors" in paths
        assert "/api/v1/ha/config" in paths


# ═══════════════════════════════════════════════════════════════════════
# Auth guards
# ═══════════════════════════════════════════════════════════════════════


class TestAuthGuards:
    """All new endpoints require authentication."""

    def test_ai_summary_requires_auth(self, client: TestClient) -> None:
        response: Response = client.post("/api/v1/ai/summary")
        assert response.status_code == 401

    def test_ai_daily_requires_auth(self, client: TestClient) -> None:
        response: Response = client.post("/api/v1/ai/summary/daily")
        assert response.status_code == 401

    def test_ha_sensors_requires_auth(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/ha/sensors")
        assert response.status_code == 401

    def test_ha_config_requires_auth(self, client: TestClient) -> None:
        response: Response = client.get("/api/v1/ha/config")
        assert response.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# Feature toggle (opt-out)
# ═══════════════════════════════════════════════════════════════════════


class TestFeatureToggle:
    """Endpoints return 404 when their feature is disabled."""

    def test_ai_endpoints_disabled(
        self, client_ai_disabled: TestClient
    ) -> None:
        """When AI_ENABLED=false, AI endpoints return 404."""
        # POST /v1/ai/summary
        resp1 = client_ai_disabled.post("/api/v1/ai/summary")
        assert resp1.status_code == 404

        resp2 = client_ai_disabled.post("/api/v1/ai/summary/daily")
        assert resp2.status_code == 404

    def test_ha_endpoints_disabled(
        self, client_ha_disabled: TestClient
    ) -> None:
        """When HA_ENABLED=false, HA endpoints return 404."""
        resp1 = client_ha_disabled.get("/api/v1/ha/sensors")
        assert resp1.status_code == 404

        resp2 = client_ha_disabled.get("/api/v1/ha/config")
        assert resp2.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# Prompt templates
# ═══════════════════════════════════════════════════════════════════════


class TestPromptTemplates:
    """Prompt templates format correctly."""

    def test_summary_prompt_format(self) -> None:
        from finance_sync.services.ai_summary import (
            _PROMPT_SUMMARY,
            _format_prompt,
        )

        data = {
            "net_worth": {"net_worth": "8000", "currency": "EUR"},
            "transactions": {"total_in_period": 10, "spending_total": "500"},
        }
        result = _format_prompt(_PROMPT_SUMMARY, data, max_length=200)
        assert "Financial Data" in result
        assert "8000" in result
        assert "{max_length}" not in result  # template vars should be replaced

    def test_daily_briefing_prompt_format(self) -> None:
        from finance_sync.services.ai_summary import (
            _PROMPT_DAILY_BRIEFING,
            _format_prompt,
        )

        data = {"net_worth": {"net_worth": "8000"}}
        result = _format_prompt(_PROMPT_DAILY_BRIEFING, data, max_length=300)
        assert "Spending since yesterday" in result
        assert "net worth change" in result or "Net worth" in result
        assert "8000" in result
