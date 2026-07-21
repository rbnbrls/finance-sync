"""Configuration helpers for the finance-sync-sdk.

Provides base classes and utilities for plugin configuration schemas.
"""

from __future__ import annotations

from pydantic import BaseModel


class PluginConfigSchema(BaseModel):
    """Base class for plugin-specific configuration schemas.

    Third-party connector and exporter plugins can subclass this to declare
    their expected configuration fields.  The host application validates
    user-provided config against the schema before passing it to the plugin.

    Usage::

        class MyBankConfig(PluginConfigSchema):
            api_key: str = Field(..., description="MyBank API key")
            sandbox: bool = Field(default=False, description="Use sandbox API")

        class MyBankPlugin(ConnectorPlugin):
            config_schema = MyBankConfig
    """

    model_config = {"extra": "forbid"}


class CredentialField:
    """Descriptor for a credential field on a config schema.

    Used in documentation / UI generation to mark which fields are
    sensitive and should be encrypted at rest.

    Usage::

        class MyBankConfig(PluginConfigSchema):
            api_key: str = Field(..., description="MyBank API key")
            endpoint: str = Field(default="https://api.mybank.com")
    """

    def __init__(
        self,
        *,
        description: str = "",
        required: bool = True,
        sensitive: bool = True,
        env_var: str | None = None,
    ) -> None:
        self.description = description
        self.required = required
        self.sensitive = sensitive
        self.env_var = env_var
