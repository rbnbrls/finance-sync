"""Connector registry with Python entry-point discovery.

Connectors are registered at build time via the ``finance_sync.connectors``
entry point group in ``pyproject.toml``::

    [project.entry-points."finance_sync.connectors"]
    bunq = "finance_sync.connectors.bunq:BunqConnector"
    trading212 = "finance_sync.connectors.trading212:Trading212Connector"

At runtime, :class:`ConnectorRegistry` discovers all installed connectors
and provides factory methods to instantiate them.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from finance_sync.connectors.base import Connector
from finance_sync.connectors.exceptions import PermanentError

if TYPE_CHECKING:
    from finance_sync.connectors.models import ConnectorConfig

_ENTRY_POINT_GROUP = "finance_sync.connectors"
_CATALOG_METADATA_KEYS = frozenset(
    {
        "auth_mode",
        "documentation_url",
        "feature_flag",
        "lifecycle_status",
    }
)


class ConnectorRegistry:
    """Discovers, validates, and instantiates connector plugins.

    Usage::

        registry = ConnectorRegistry()
        connector = registry.get_connector(config)
        await connector.authenticate()
        accounts = await connector.fetch_accounts()

    The registry caches discovered connector classes.  Call
    :meth:`reload` to re-scan entry points after installing new packages.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[Connector]] = {}
        self._loaded = False

    # ── Discovery ──────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-scan installed packages for finance_sync.connectors
        entry points.

        Clears the cached class map and re-discovers all connectors.
        """
        self._classes.clear()
        self._loaded = False
        self._load_once()

    def _load_once(self) -> None:
        if self._loaded:
            return

        for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
            name = ep.name
            try:
                cls = ep.load()
                if not issubclass(cls, Connector):
                    msg = (
                        f"Entry point {name!r} does not resolve to a "
                        f"Connector subclass (got {cls!r})"
                    )
                    raise TypeError(msg)
                if name in self._classes:
                    msg = (
                        f"Duplicate connector key {name!r} -- "
                        f"{ep.module}.{ep.attr} conflicts with "
                        f"{self._classes[name]}"
                    )
                    raise ValueError(msg)
                self._classes[name] = cls
            except Exception as exc:
                msg = f"Failed to load connector entry point {name!r}: {exc}"
                raise RuntimeError(msg) from exc

        self._loaded = True

    @staticmethod
    def _plugin_package(
        connector_cls: type[Connector], entry_point: Any = None
    ) -> str:
        """Return a safe distribution/module label for catalogues."""
        distribution = getattr(entry_point, "dist", None)
        distribution_name = getattr(distribution, "name", None)
        if isinstance(distribution_name, str) and distribution_name.strip():
            return distribution_name
        module = getattr(connector_cls, "__module__", "")
        return module.split(".", 1)[0] or "unknown"

    @classmethod
    def _catalog_metadata(
        cls, name: str, connector: type[Connector], entry_point: Any = None
    ) -> dict[str, Any]:
        """Build a validated, secret-safe metadata record.

        Connector metadata is optional extension data.  A malformed value
        must not prevent the connector from being loaded or the catalogue
        from being rendered.
        """
        raw = getattr(connector, "catalog_metadata", {})
        metadata_incomplete = not isinstance(raw, dict)
        values = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
        safe: dict[str, Any] = {}
        for key in _CATALOG_METADATA_KEYS:
            value = values.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                metadata_incomplete = True
                continue
            safe[key] = value.strip()

        rate_policy = getattr(connector, "rate_limit_policy", None)
        rate_limit: dict[str, Any] | None = None
        if rate_policy is not None:
            rate_values = {
                "max_requests": getattr(rate_policy, "max_requests", None),
                "window_seconds": getattr(rate_policy, "window_seconds", None),
                "max_retries": getattr(rate_policy, "max_retries", None),
            }
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in rate_values.values()
            ):
                rate_limit = rate_values
            else:
                metadata_incomplete = True

        supported: object = getattr(
            connector, "supported_resources", frozenset[str]()
        )
        if not isinstance(supported, (set, frozenset, list, tuple)):
            metadata_incomplete = True
            supported_resources: list[str] = []
        else:
            supported_values = list(cast("Iterable[object]", supported))
            if not all(
                isinstance(item, str) and item for item in supported_values
            ):
                metadata_incomplete = True
                supported_resources = []
            else:
                supported_resources = sorted(
                    set(cast("list[str]", supported_values))
                )

        result: dict[str, Any] = {
            "name": name,
            "provider_key": name,
            "display_name": getattr(connector, "display_name", "") or name,
            "plugin_package": cls._plugin_package(connector, entry_point),
            "plugin_version": getattr(connector, "plugin_version", "0.1.0"),
            "sdk_version": getattr(connector, "sdk_version", "0.1.0"),
            "supported_resources": supported_resources,
            "rate_limit_policy": rate_limit,
            "has_rate_limit_policy": rate_limit is not None,
            "metadata_incomplete": metadata_incomplete,
        }
        result.update(safe)
        return result

    # ── Registration ───────────────────────────────────────────────────

    def register_class(
        self,
        name: str,
        cls: type[Connector],
        replace: bool = False,
    ) -> None:
        """Register a connector class under *name*.

        Raises ``ValueError`` if *name* is already registered unless
        *replace* is ``True``.
        """
        if not issubclass(cls, Connector):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"Expected a Connector subclass, got {cls!r}"
            raise TypeError(msg)
        if name in self._classes and not replace:
            msg = (
                f"Connector {name!r} is already registered as "
                f"{self._classes[name]}"
            )
            raise ValueError(msg)
        self._classes[name] = cls

    # ── Factory ────────────────────────────────────────────────────────

    def get_connector(self, config: ConnectorConfig) -> Connector:
        """Instantiate a connector for the given *config*.

        The *config.provider_type* must match a registered connector name.
        """
        self._load_once()

        cls = self._classes.get(config.provider_type)
        if cls is None:
            known = sorted(self._classes)
            msg = (
                f"Unknown connector {config.provider_type!r}. "
                f"Available: {known}"
            )
            raise PermanentError(msg)

        return cls(config=config)

    # ── Metadata ───────────────────────────────────────────────────────

    def list_connectors(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all discovered connectors.

        Returns a dict keyed by connector name with class-level metadata::

            {
                "bunq": {
                    "name": "bunq",
                    "display_name": "",
                    "sdk_version": "0.1.0",
                    "has_rate_limit_policy": False,
                },
                ...
            }
        """
        self._load_once()
        result: dict[str, dict[str, Any]] = {}
        for name, cls in self._classes.items():
            result[name] = self._catalog_metadata(name, cls)
        return result

    @property
    def available(self) -> list[str]:
        """Sorted list of registered connector names."""
        self._load_once()
        return sorted(self._classes)

    def __contains__(self, name: str) -> bool:
        self._load_once()
        return name in self._classes

    def __len__(self) -> int:
        self._load_once()
        return len(self._classes)
