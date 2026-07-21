"""Plugin registry with Python entry-point discovery.

Connector and exporter plugins are registered at build time via entry-point
groups in ``pyproject.toml``:

.. code-block:: toml

    [project.entry-points."finance_sync_sdk.plugins"]
    mybank = "mybank_finance_sync:MyBankPlugin"

At runtime, :class:`PluginRegistry` discovers all installed plugins
and provides factory methods to instantiate them.
"""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync_sdk.models import ConnectorConfig
    from finance_sync_sdk.plugin import ConnectorPlugin, ExporterPlugin

_PLUGIN_ENTRY_POINT = "finance_sync_sdk.plugins"
_EXPORTER_ENTRY_POINT = "finance_sync_sdk.exporters"


class PluginRegistry:
    """Discovers, validates, and instantiates connector and exporter plugins.

    Usage::

        registry = PluginRegistry()
        plugin = registry.get_connector(connector_config)
        await plugin.authenticate()

    The registry caches discovered plugin classes.  Call
    :meth:`reload` to re-scan entry points after installing new packages.
    """

    def __init__(self) -> None:
        self._connector_classes: dict[str, type[ConnectorPlugin]] = {}
        self._exporter_classes: dict[str, type[ExporterPlugin]] = {}
        self._loaded = False

    # ── Discovery ──────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-scan installed packages for plugin entry points.

        Clears the cached class map and re-discovers all plugins.
        """
        self._connector_classes.clear()
        self._exporter_classes.clear()
        self._loaded = False
        self._load_once()

    def _load_once(self) -> None:
        if self._loaded:
            return

        self._load_plugins()
        self._load_exporters()
        self._loaded = True

    def _load_plugins(self) -> None:
        from finance_sync_sdk.plugin import ConnectorPlugin

        for ep in importlib.metadata.entry_points(group=_PLUGIN_ENTRY_POINT):
            name = ep.name
            try:
                cls = ep.load()
                if not issubclass(cls, ConnectorPlugin):
                    msg = (
                        f"Entry point {name!r} does not resolve to a "
                        f"ConnectorPlugin subclass (got {cls!r})"
                    )
                    raise TypeError(msg)
                if name in self._connector_classes:
                    msg = (
                        f"Duplicate plugin key {name!r} — "
                        f"{ep.module}.{ep.attr} conflicts with "
                        f"{self._connector_classes[name]}"
                    )
                    raise ValueError(msg)
                self._connector_classes[name] = cls
            except Exception as exc:
                msg = f"Failed to load plugin entry point {name!r}: {exc}"
                raise RuntimeError(msg) from exc

    def _load_exporters(self) -> None:
        from finance_sync_sdk.plugin import ExporterPlugin

        for ep in importlib.metadata.entry_points(group=_EXPORTER_ENTRY_POINT):
            name = ep.name
            try:
                cls = ep.load()
                if not issubclass(cls, ExporterPlugin):
                    msg = (
                        f"Entry point {name!r} does not resolve to an "
                        f"ExporterPlugin subclass (got {cls!r})"
                    )
                    raise TypeError(msg)
                if name in self._exporter_classes:
                    msg = (
                        f"Duplicate exporter key {name!r} — "
                        f"{ep.module}.{ep.attr} conflicts with "
                        f"{self._exporter_classes[name]}"
                    )
                    raise ValueError(msg)
                self._exporter_classes[name] = cls
            except Exception as exc:
                msg = f"Failed to load exporter entry point {name!r}: {exc}"
                raise RuntimeError(msg) from exc

    # ── Registration (for in-process / testing) ────────────────────────

    def register_connector(
        self,
        name: str,
        cls: type,
        replace: bool = False,
    ) -> None:
        """Register a connector plugin class under *name*.

        Raises ``ValueError`` if *name* is already registered unless
        *replace* is ``True``.
        """
        from finance_sync_sdk.plugin import ConnectorPlugin

        if not issubclass(cls, ConnectorPlugin):
            msg = f"Expected a ConnectorPlugin subclass, got {cls!r}"
            raise TypeError(msg)
        if name in self._connector_classes and not replace:
            msg = (
                f"Connector plugin {name!r} is already registered as "
                f"{self._connector_classes[name]}"
            )
            raise ValueError(msg)
        self._connector_classes[name] = cls

    def register_exporter(
        self,
        name: str,
        cls: type,
        replace: bool = False,
    ) -> None:
        """Register an exporter plugin class under *name*."""
        from finance_sync_sdk.plugin import ExporterPlugin

        if not issubclass(cls, ExporterPlugin):
            msg = f"Expected an ExporterPlugin subclass, got {cls!r}"
            raise TypeError(msg)
        if name in self._exporter_classes and not replace:
            msg = (
                f"Exporter plugin {name!r} is already registered as {self._exporter_classes[name]}"
            )
            raise ValueError(msg)
        self._exporter_classes[name] = cls

    # ── Factory ────────────────────────────────────────────────────────

    def get_connector(self, config: ConnectorConfig) -> ConnectorPlugin:
        """Instantiate a connector plugin for the given *config*.

        The *config.provider_type* must match a registered plugin name.
        """
        self._load_once()

        cls = self._connector_classes.get(config.provider_type)
        if cls is None:
            known = sorted(self._connector_classes)
            msg = f"Unknown connector plugin {config.provider_type!r}. Available: {known}"
            raise RuntimeError(msg)

        return cls(config=config)

    def get_exporter(self, name: str, config: object | None = None) -> ExporterPlugin:
        """Instantiate an exporter plugin by *name*.

        If *config* is provided, it is passed to the constructor.
        """
        self._load_once()

        cls = self._exporter_classes.get(name)
        if cls is None:
            known = sorted(self._exporter_classes)
            msg = f"Unknown exporter plugin {name!r}. Available: {known}"
            raise RuntimeError(msg)

        return cls(config=config)

    # ── Metadata ───────────────────────────────────────────────────────

    def list_connectors(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all discovered connector plugins."""
        self._load_once()
        result: dict[str, dict[str, Any]] = {}
        for name, cls in self._connector_classes.items():
            result[name] = {
                "name": name,
                "display_name": getattr(cls, "display_name", ""),
                "plugin_version": getattr(cls, "plugin_version", "0.1.0"),
                "has_rate_limit_policy": (getattr(cls, "rate_limit_policy", None) is not None),
            }
        return result

    def list_exporters(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all discovered exporter plugins."""
        self._load_once()
        result: dict[str, dict[str, Any]] = {}
        for name, cls in self._exporter_classes.items():
            result[name] = {
                "name": name,
                "display_name": getattr(cls, "display_name", ""),
                "plugin_version": getattr(cls, "plugin_version", "0.1.0"),
                "supported_formats": getattr(cls, "supported_formats", None),
            }
        return result

    #: Sorted list of registered connector plugin names.
    @property
    def available_connectors(self) -> list[str]:
        self._load_once()
        return sorted(self._connector_classes)

    #: Sorted list of registered exporter plugin names.
    @property
    def available_exporters(self) -> list[str]:
        self._load_once()
        return sorted(self._exporter_classes)

    def __contains__(self, name: str) -> bool:
        self._load_once()
        return name in self._connector_classes or name in self._exporter_classes

    def __len__(self) -> int:
        self._load_once()
        return len(self._connector_classes) + len(self._exporter_classes)
