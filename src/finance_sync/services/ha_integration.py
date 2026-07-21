"""Home Assistant REST API compatible sensor integration.

Exposes financial data (net worth, portfolio value, account count,
last sync time, sync health) as a Home Assistant REST sensor endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.services.read_api import ReadService

if TYPE_CHECKING:
    from decimal import Decimal

logger = structlog.get_logger(__name__)


# ── Sensor definitions ────────────────────────────────────────────


class HASensor:
    """A Home Assistant REST sensor value."""

    def __init__(
        self,
        *,
        sensor_id: str,
        name: str,
        value: str | None,
        unit_of_measurement: str | None = None,
        icon: str | None = None,
        state_class: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.sensor_id = sensor_id
        self.name = name
        self.value = value
        self.unit_of_measurement = unit_of_measurement
        self.icon = icon
        self.state_class = state_class
        self.attributes = attributes

    def to_dict(self) -> dict[str, Any]:
        """Return the sensor dict as consumed by the HA REST sensor API.

        Home Assistant REST sensor format:
        https://www.home-assistant.io/integrations/rest/
        """
        result: dict[str, Any] = {
            "state": self.value,
            "attributes": {
                "friendly_name": self.name,
            },
        }
        if self.unit_of_measurement is not None:
            result["attributes"]["unit_of_measurement"] = (
                self.unit_of_measurement
            )
        if self.icon is not None:
            result["attributes"]["icon"] = self.icon
        if self.state_class is not None:
            result["attributes"]["state_class"] = self.state_class
        if self.attributes:
            result["attributes"].update(self.attributes)
        return result


# ── HA integration config ─────────────────────────────────────────


class HAConfigResponse:
    """Configuration for Home Assistant REST sensor integration."""

    def __init__(self, *, base_url: str, sensor_ids: list[str]) -> None:
        self.base_url = base_url
        self.sensor_ids = sensor_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "sensor_ids": self.sensor_ids,
            "poll_interval_seconds": 300,  # 5 minutes
            "authentication": {
                "type": "api_key",
                "header_name": "X-API-Key",
            },
            "notes": (
                "Add these as REST sensors in Home Assistant's "
                "configuration.yaml using the 'X-API-Key' header. "
                "Example sensor configuration is in each sensor's HA config."
            ),
        }


# ── Service ───────────────────────────────────────────────────────


class HomeAssistantService:
    """Exposes financial metrics as Home Assistant REST sensor values.

    Each sensor fetches data from the ReadService and formats it
    in the HA REST sensor format.
    """

    def __init__(self, session: Any, settings: Any) -> None:
        self._session = session
        self._settings = settings
        self._read_service = ReadService(session)
        self._log = logger.bind(service="ha_integration")

    async def get_sensors(self, tenant_id: str) -> list[HASensor]:
        """Return all HA sensors for a tenant.

        Gathers net worth, portfolio value, account count, last sync
        time, and sync health status in parallel.
        """

        from sqlalchemy import func, select

        from finance_sync.models.sync_run import SyncRun

        self._log.info("ha_sensors_gathering", tenant_id=tenant_id)

        # Gather data
        net_worth_data = await self._read_service.get_net_worth(tenant_id)
        portfolio_data = await self._read_service.get_portfolio(tenant_id)
        accounts_data = await self._read_service.list_accounts(
            tenant_id, limit=100
        )

        # Last successful sync run
        last_sync_q = (
            select(SyncRun)
            .where(
                SyncRun.status == "completed",  # type: ignore[attr-defined]
            )
            .order_by(SyncRun.started_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        sync_result = await self._session.execute(last_sync_q)
        last_sync: SyncRun | None = sync_result.scalar_one_or_none()  # type: ignore[assignment]

        # Recent failed sync runs (last 24h)
        since_24h = datetime.now(UTC)
        failed_q = (
            select(func.count())
            .select_from(SyncRun)
            .where(
                SyncRun.status == "failed",  # type: ignore[attr-defined]
                SyncRun.started_at >= since_24h,  # type: ignore[attr-defined]
            )
        )
        failed_result = await self._session.execute(failed_q)
        failed_count: int = failed_result.scalar() or 0  # type: ignore[assignment]

        # Build sensor list
        sensors: list[HASensor] = []

        # 1. Net worth
        net_worth_val = _fmt_decimal(net_worth_data.net_worth)
        sensors.append(
            HASensor(
                sensor_id="sensor.finance_sync_net_worth",
                name="Finance Sync Net Worth",
                value=net_worth_val,
                unit_of_measurement="EUR",
                icon="mdi:currency-eur",
                state_class="measurement",
            )
        )

        # 2. Portfolio value
        portfolio_val = _fmt_decimal(portfolio_data.total_value)
        sensors.append(
            HASensor(
                sensor_id="sensor.finance_sync_portfolio_value",
                name="Finance Sync Portfolio Value",
                value=portfolio_val,
                unit_of_measurement="EUR",
                icon="mdi:chart-line",
                state_class="measurement",
            )
        )

        # 3. Account count
        sensors.append(
            HASensor(
                sensor_id="sensor.finance_sync_account_count",
                name="Finance Sync Account Count",
                value=str(accounts_data.total),
                icon="mdi:bank",
                state_class="measurement",
            )
        )

        # 4. Last sync time
        if last_sync is not None:
            last_sync_str = (
                last_sync.started_at.isoformat()
                if last_sync.started_at
                else "unknown"
            )
        else:
            last_sync_str = "never"
        sensors.append(
            HASensor(
                sensor_id="sensor.finance_sync_last_sync",
                name="Finance Sync Last Sync",
                value=last_sync_str,
                icon="mdi:sync",
                attributes={
                    "last_sync_connector": last_sync.connector
                    if last_sync
                    else None,
                },
            )
        )

        # 5. Sync health status
        if last_sync is not None:
            health = (
                "healthy"
                if failed_count == 0
                else "degraded"
                if failed_count <= 3
                else "unhealthy"
            )
        else:
            health = "never_synced"

        sensors.append(
            HASensor(
                sensor_id="sensor.finance_sync_sync_status",
                name="Finance Sync Status",
                value=health,
                icon="mdi:heart-pulse",
                attributes={
                    "failed_syncs_24h": failed_count,
                    "last_sync_status": last_sync.status if last_sync else None,
                },
            )
        )

        return sensors

    def get_config(self, base_url: str) -> HAConfigResponse:
        """Return the HA integration configuration."""
        return HAConfigResponse(
            base_url=base_url,
            sensor_ids=[
                "sensor.finance_sync_net_worth",
                "sensor.finance_sync_portfolio_value",
                "sensor.finance_sync_account_count",
                "sensor.finance_sync_last_sync",
                "sensor.finance_sync_sync_status",
            ],
        )


def _fmt_decimal(val: Decimal | None) -> str:
    """Format a Decimal to a string for HA sensor value."""
    if val is None:
        return "0"
    return f"{val:.2f}"
