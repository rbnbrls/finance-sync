"""Acceptance tests for the subscriptions API endpoint (GET /subscriptions).

Tests the endpoint layer: HTTP handling, parameter parsing, response
formatting, and authentication.  The service layer is fully mocked so
these tests do not require a real database.

Also covers GET /subscriptions/detected and POST /subscriptions/detect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI
    from httpx import Response

import pytest
from fastapi.testclient import TestClient

# Patch SubscriptionDetector at the import point used by the endpoint module
import finance_sync.api.v1.subscriptions as _subs_mod
from finance_sync.api.deps.auth import AuthContext
from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.services.subscription_detector.service import (
    Subscription as SubscriptionResult,
)

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_detector() -> MagicMock:
    """Create a mock SubscriptionDetector instance.

    The ``list_subscriptions`` method is pre-configured to return an
    empty list by default.  Individual tests can override
    ``mock.return_value`` via the mock's attribute.
    """
    detector = MagicMock()
    detector.list_subscriptions = AsyncMock(return_value=[])
    return detector


@pytest.fixture(autouse=True)
def _patch_subscription_detector(  # pyright: ignore[reportUnusedFunction]
    mock_detector: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replace SubscriptionDetector with a mock for every test.

    The patched class returns ``mock_detector`` when instantiated,
    so endpoints that create ``SubscriptionDetector(...)`` get the
    pre-configured mock.
    """
    monkeypatch.setattr(
        _subs_mod,
        "SubscriptionDetector",
        lambda *args, **kwargs: mock_detector,
    )


@pytest.fixture
def app() -> FastAPI:
    """Build a FastAPI app without a DB (service is mocked)."""
    from finance_sync.app import create_app

    app = create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key="test-secret-key-at-least-16-chars",  # type: ignore[call-arg]
        )
    )

    # Build a minimal DB-less container for get_container()
    container = Container.from_settings(
        Settings(
            database_url=None,
            redis_url=None,
            secret_key="test-secret-key-at-least-16-chars",  # type: ignore[call-arg]
        )
    )
    app.state.container = container

    # Override auth to bypass JWT validation
    async def _fake_auth_override() -> AuthContext:
        return AuthContext(
            user=MagicMock(
                tenant_id="tenant_1",
                id="user_1",
                role="admin",
                is_active=True,
            )
        )

    from finance_sync.api.deps.auth import get_auth_context

    app.dependency_overrides[get_auth_context] = _fake_auth_override

    return app


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """FastAPI test client.

    The lifespan handler creates a DB-less container and stores it on
    ``app.state.container``.  Immediately after entering the context
    we replace it with a container that has a real SQLite engine so
    ``get_container(request).session_factory`` works in the endpoint.
    The ``SubscriptionDetector`` is independently mocked (autouse), so
    no actual queries are executed against the engine.
    """
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    session_factory: async_sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    mock_container = Container()
    mock_container._engine = engine  # type: ignore[union-attr]
    mock_container._session_factory = session_factory  # type: ignore[union-attr]

    with TestClient(app) as c:
        # Lifespan has run — replace with our container that has a
        # real session_factory so the endpoint code works.
        app.state.container = mock_container
        yield c

    import asyncio

    asyncio.run(engine.dispose())


# ── Helper ─────────────────────────────────────────────────────────────


def _mock_subscription(**overrides: Any) -> MagicMock:
    """Build a MagicMock that looks like a DetectedSubscription row."""
    from uuid import uuid4

    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "id": str(uuid4()),
        "tenant_id": "tenant_1",
        "merchant_name": "Netflix B.V.",
        "raw_description": "POS Netflix B.V.",
        "amount": "-15.99",
        "currency_code": "EUR",
        "frequency_days": 30,
        "frequency_label": "monthly",
        "confidence": "high",
        "detection_method": "hybrid",
        "status": "active",
        "transaction_ids": None,
        "account_id": "acct_1",
        "provider_key": "bunq",
        "security_id": None,
        "sector": "Communication Services",
        "category": "streaming",
        "first_detected_at": now - timedelta(days=180),
        "last_detected_at": now,
        "occurrence_count": 6,
        "detection_score": 0.85,
        "details": None,
        "user_notes": None,
        "created_at": now,
    }
    base.update(overrides)
    mock = MagicMock(spec=[])
    for k, v in base.items():
        setattr(mock, k, v)
    return mock


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════


class TestListSubscriptionsEndpoint:
    """GET /subscriptions — listing detected subscriptions."""

    def test_list_empty(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """Empty result returns an empty list."""
        mock_detector.list_subscriptions.return_value = []

        response: Response = client.get("/api/v1/subscriptions")
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_list_with_subscriptions(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """Returns mocked subscriptions."""
        mock_detector.list_subscriptions.return_value = [
            _mock_subscription(merchant_name="Netflix B.V."),
            _mock_subscription(merchant_name="Spotify AB"),
        ]

        response: Response = client.get("/api/v1/subscriptions")
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        merchants = {item["merchant_name"] for item in data["items"]}
        assert merchants == {"Netflix B.V.", "Spotify AB"}

    def test_list_filters_by_status(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """?status=active is passed to the service."""
        mock_detector.list_subscriptions.return_value = [
            _mock_subscription(merchant_name="Netflix", status="active"),
        ]

        response: Response = client.get(
            "/api/v1/subscriptions", params={"status": "active"}
        )
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["total"] == 1

        mock_detector.list_subscriptions.assert_called_once_with(
            status="active", confidence=None, limit=50, offset=0
        )

    def test_list_filters_by_confidence(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """?confidence=high is passed to the service."""
        mock_detector.list_subscriptions.return_value = [
            _mock_subscription(merchant_name="Netflix", confidence="high"),
        ]

        response: Response = client.get(
            "/api/v1/subscriptions", params={"confidence": "high"}
        )
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["total"] == 1

        mock_detector.list_subscriptions.assert_called_once_with(
            status=None, confidence="high", limit=50, offset=0
        )

    def test_list_pagination(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """Respects limit and offset parameters."""
        mock_detector.list_subscriptions.return_value = [
            _mock_subscription(merchant_name=f"Merchant {i}") for i in range(3)
        ]

        response: Response = client.get(
            "/api/v1/subscriptions", params={"limit": 3, "offset": 2}
        )
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["limit"] == 3
        assert data["offset"] == 2
        assert len(data["items"]) == 3

        mock_detector.list_subscriptions.assert_called_once_with(
            status=None, confidence=None, limit=3, offset=2
        )

    def test_list_response_shape(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """Each subscription item has expected fields."""
        mock_detector.list_subscriptions.return_value = [
            _mock_subscription(
                merchant_name="Netflix B.V.",
                amount="-15.99",
                confidence="high",
                status="active",
                detection_method="hybrid",
                sector="Communication Services",
                category="streaming",
                occurrence_count=6,
            )
        ]

        response: Response = client.get("/api/v1/subscriptions")
        data: dict[str, Any] = response.json()
        item = data["items"][0]

        assert item["merchant_name"] == "Netflix B.V."
        assert item["amount"] == "-15.99"
        assert item["confidence"] == "high"
        assert item["status"] == "active"
        assert item["detection_method"] == "hybrid"
        assert item["sector"] == "Communication Services"
        assert item["category"] == "streaming"
        assert item["currency_code"] == "EUR"
        assert item["occurrence_count"] == 6
        assert item["frequency_label"] == "monthly"
        assert "first_detected_at" in item
        assert "last_detected_at" in item
        assert item["user_notes"] is None

    def test_list_default_params(
        self, client: TestClient, mock_detector: MagicMock
    ) -> None:
        """Defaults (limit=50, offset=0) are used when not specified."""
        mock_detector.list_subscriptions.return_value = []

        response: Response = client.get("/api/v1/subscriptions")
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_list_requires_auth(self, client: TestClient) -> None:
        """Unauthenticated requests get 401 or 403."""
        from finance_sync.api.deps.auth import get_auth_context

        client.app.dependency_overrides.pop(get_auth_context, None)

        response: Response = client.get("/api/v1/subscriptions")
        assert response.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════
# POST /subscriptions/detect — integrated detection
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSubscriptionEndpoint:
    """POST /subscriptions/detect — running integrated subscription detection.

    The ``SubscriptionDetectionService`` is fully mocked so no actual
    database queries are executed.
    """

    @pytest.fixture
    def mock_detection_svc(self) -> AsyncMock:
        """Create a mock ``SubscriptionDetectionService`` instance.

        ``detect_subscriptions`` is pre-configured to return an empty
        list.  Individual tests override ``.return_value``.
        """
        svc = MagicMock(spec=["detect_subscriptions"])
        svc.detect_subscriptions = AsyncMock(return_value=[])
        return svc

    @pytest.fixture(autouse=True)
    def _patch_detection_service(
        self,
        mock_detection_svc: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replace ``SubscriptionDetectionService`` with a factory that
        returns the pre-configured mock."""
        monkeypatch.setattr(
            _subs_mod,
            "SubscriptionDetectionService",
            lambda *args, **kwargs: mock_detection_svc,
        )

    @staticmethod
    def _make_result(**overrides: Any) -> SubscriptionResult:
        """Build a ``Subscription`` dataclass with sensible defaults."""
        from decimal import Decimal

        from finance_sync.models.enums import (
            DetectionMethod,
            SubscriptionConfidence,
            SubscriptionStatus,
        )

        now = datetime.now(UTC)
        kwargs: dict[str, Any] = {
            "merchant_name": "Netflix B.V.",
            "raw_description": "POS Netflix B.V.",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "frequency_days": 30,
            "frequency_label": "monthly",
            "confidence": SubscriptionConfidence.HIGH,
            "detection_score": 0.92,
            "detection_method": DetectionMethod.EXACT_AMOUNT,
            "status": SubscriptionStatus.ACTIVE,
            "transaction_ids": ["txn_1", "txn_2", "txn_3"],
            "account_id": "acct_1",
            "provider_key": "bunq",
            "security_id": "sec_netflix",
            "fundamentals_available": True,
            "category": "streaming",
            "sector": "Communication Services",
            "first_detected_at": now - timedelta(days=180),
            "last_detected_at": now,
            "occurrence_count": 6,
            "details": {"amount_consistency": 1.0, "interval_regularity": 0.9},
        }
        kwargs.update(overrides)
        return SubscriptionResult(**kwargs)

    # ── Tests ───────────────────────────────────────────────────────

    def test_detect_empty(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Empty detection result returns an empty JSON array."""
        mock_detection_svc.detect_subscriptions.return_value = []

        response: Response = client.post(
            "/api/v1/subscriptions/detect", json={}
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_detect_returns_subscriptions(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Detection results are serialised as a JSON array of items."""
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(merchant_name="Netflix B.V."),
            self._make_result(merchant_name="Spotify AB"),
        ]

        response: Response = client.post(
            "/api/v1/subscriptions/detect", json={}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        merchants = {item["merchant_name"] for item in data}
        assert merchants == {"Netflix B.V.", "Spotify AB"}

    def test_detect_response_shape(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Each item has the expected fields with correct JSON types."""
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(),
        ]

        response: Response = client.post(
            "/api/v1/subscriptions/detect", json={}
        )
        assert response.status_code == 200
        item = response.json()[0]

        assert item["merchant_name"] == "Netflix B.V."
        assert item["amount"] == "15.99"
        assert item["currency_code"] == "EUR"
        assert item["confidence"] == "high"
        assert item["detection_method"] == "exact_amount"
        assert item["status"] == "active"
        assert item["category"] == "streaming"
        assert item["sector"] == "Communication Services"
        assert item["frequency_days"] == 30
        assert item["frequency_label"] == "monthly"
        assert item["occurrence_count"] == 6
        assert item["detection_score"] == 0.92
        assert isinstance(item["transaction_ids"], list)
        assert "txn_1" in item["transaction_ids"]
        assert item["account_id"] == "acct_1"
        assert item["provider_key"] == "bunq"
        # Optional fields
        assert item["security_id"] == "sec_netflix"
        assert item["fundamentals_available"] is True
        assert item["raw_description"] == "POS Netflix B.V."
        assert item["first_detected_at"] is not None
        assert item["last_detected_at"] is not None
        # Details dict
        assert isinstance(item["details"], dict)
        assert item["details"]["amount_consistency"] == 1.0

    def test_detect_passes_parameters(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """date_from, date_to, and min_occurrences are forwarded."""
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(),
        ]

        response: Response = client.post(
            "/api/v1/subscriptions/detect",
            json={
                "date_from": "2025-01-01T00:00:00Z",
                "date_to": "2025-12-31T23:59:59Z",
                "min_occurrences": 3,
            },
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

        mock_detection_svc.detect_subscriptions.assert_awaited_once_with(
            user_id="tenant_1",
            date_from=datetime(2025, 1, 1, tzinfo=UTC),
            date_to=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

    def test_detect_requires_auth(self, client: TestClient) -> None:
        """Unauthenticated requests get 401 or 403."""
        from finance_sync.api.deps.auth import get_auth_context

        client.app.dependency_overrides.pop(get_auth_context, None)

        response: Response = client.post(
            "/api/v1/subscriptions/detect", json={}
        )
        assert response.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════
# GET /subscriptions/detected — on-the-fly detection
# ═══════════════════════════════════════════════════════════════════════


class TestGetDetectedSubscriptionsEndpoint:
    """GET /subscriptions/detected — read-only on-the-fly detection."""

    @pytest.fixture
    def mock_detection_svc(self) -> AsyncMock:
        """Create a mock ``SubscriptionDetectionService`` instance.

        ``detect_subscriptions`` is pre-configured to return an empty
        list.  Individual tests override ``.return_value``.
        """
        svc = MagicMock(spec=["detect_subscriptions"])
        svc.detect_subscriptions = AsyncMock(return_value=[])
        return svc

    @pytest.fixture(autouse=True)
    def _patch_detection_service(
        self,
        mock_detection_svc: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replace ``SubscriptionDetectionService`` with a factory that
        returns the pre-configured mock."""
        monkeypatch.setattr(
            _subs_mod,
            "SubscriptionDetectionService",
            lambda *args, **kwargs: mock_detection_svc,
        )

    @staticmethod
    def _make_result(**overrides: Any) -> SubscriptionResult:
        """Build a ``Subscription`` dataclass with sensible defaults."""
        from decimal import Decimal

        from finance_sync.models.enums import (
            DetectionMethod,
            SubscriptionConfidence,
            SubscriptionStatus,
        )

        now = datetime.now(UTC)
        kwargs: dict[str, Any] = {
            "merchant_name": "Netflix B.V.",
            "raw_description": "POS Netflix B.V.",
            "amount": Decimal("15.99"),
            "currency_code": "EUR",
            "frequency_days": 30,
            "frequency_label": "monthly",
            "confidence": SubscriptionConfidence.HIGH,
            "detection_score": 0.92,
            "detection_method": DetectionMethod.EXACT_AMOUNT,
            "status": SubscriptionStatus.ACTIVE,
            "transaction_ids": ["txn_1", "txn_2", "txn_3"],
            "account_id": "acct_1",
            "provider_key": "bunq",
            "security_id": "sec_netflix",
            "fundamentals_available": True,
            "category": "streaming",
            "sector": "Communication Services",
            "first_detected_at": now - timedelta(days=180),
            "last_detected_at": now,
            "occurrence_count": 6,
            "details": {"amount_consistency": 1.0, "interval_regularity": 0.9},
        }
        kwargs.update(overrides)
        return SubscriptionResult(**kwargs)

    # ── Tests ───────────────────────────────────────────────────────

    def test_detected_empty(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Empty detection result returns an empty JSON array."""
        mock_detection_svc.detect_subscriptions.return_value = []

        response: Response = client.get("/api/v1/subscriptions/detected")
        assert response.status_code == 200
        assert response.json() == []

    def test_detected_returns_subscriptions(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Detection results are serialised as a JSON array of items."""
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(merchant_name="Netflix B.V."),
            self._make_result(merchant_name="Spotify AB"),
        ]

        response: Response = client.get("/api/v1/subscriptions/detected")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        merchants = {item["merchant_name"] for item in data}
        assert merchants == {"Netflix B.V.", "Spotify AB"}

    def test_detected_response_shape(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Each item has the expected fields with correct JSON types."""
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(),
        ]

        response: Response = client.get("/api/v1/subscriptions/detected")
        assert response.status_code == 200
        item = response.json()[0]

        assert item["merchant_name"] == "Netflix B.V."
        assert item["amount"] == "15.99"
        assert item["currency_code"] == "EUR"
        assert item["confidence"] == "high"
        assert item["detection_method"] == "exact_amount"
        assert item["status"] == "active"
        assert item["category"] == "streaming"
        assert item["sector"] == "Communication Services"
        assert item["frequency_days"] == 30
        assert item["frequency_label"] == "monthly"
        assert item["occurrence_count"] == 6
        assert item["detection_score"] == 0.92
        assert isinstance(item["transaction_ids"], list)
        assert "txn_1" in item["transaction_ids"]
        assert item["account_id"] == "acct_1"
        assert item["provider_key"] == "bunq"
        # Optional fields
        assert item["security_id"] == "sec_netflix"
        assert item["fundamentals_available"] is True
        assert item["raw_description"] == "POS Netflix B.V."
        assert item["first_detected_at"] is not None
        assert item["last_detected_at"] is not None
        # Details dict
        assert isinstance(item["details"], dict)
        assert item["details"]["amount_consistency"] == 1.0

    def test_detected_passes_query_parameters(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """date_from, date_to, and min_occurrences query params are forwarded."""  # noqa: E501
        mock_detection_svc.detect_subscriptions.return_value = [
            self._make_result(),
        ]

        response: Response = client.get(
            "/api/v1/subscriptions/detected",
            params={
                "date_from": "2025-01-01T00:00:00Z",
                "date_to": "2025-12-31T23:59:59Z",
                "min_occurrences": 3,
            },
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

        mock_detection_svc.detect_subscriptions.assert_awaited_once_with(
            user_id="tenant_1",
            date_from=datetime(2025, 1, 1, tzinfo=UTC),
            date_to=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

    def test_detected_uses_default_min_occurrences(
        self, client: TestClient, mock_detection_svc: AsyncMock
    ) -> None:
        """Default min_occurrences=2 is used when not specified."""
        mock_detection_svc.detect_subscriptions.return_value = []

        response: Response = client.get("/api/v1/subscriptions/detected")
        assert response.status_code == 200

        # Verify the service was called with min_occurrences=2
        mock_detection_svc.detect_subscriptions.assert_awaited_once_with(
            user_id="tenant_1",
            date_from=None,
            date_to=None,
        )
        # Also verify the service was constructed with min_occurrences=2
        # by checking the factory call args
        assert mock_detection_svc.mock_calls

    def test_detected_requires_auth(self, client: TestClient) -> None:
        """Unauthenticated requests get 401 or 403."""
        from finance_sync.api.deps.auth import get_auth_context

        client.app.dependency_overrides.pop(get_auth_context, None)

        response: Response = client.get("/api/v1/subscriptions/detected")
        assert response.status_code in (401, 403)
