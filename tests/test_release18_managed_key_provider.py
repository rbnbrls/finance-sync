"""Release 18 managed-key provider contracts."""

# pyright: basic

import json
from pathlib import Path

import pytest

from finance_sync.services.key_provider import (
    KeyProviderError,
    KeyVersion,
    LocalTestKeyProvider,
    ManagedKeyProvider,
)


def test_local_testdouble_supports_rotation_states_without_material_audit() -> None:
    keys = {
        "v1": KeyVersion("v1", "previous", b"1" * 32),
        "v2": KeyVersion("v2", "current", b"2" * 32),
        "v0": KeyVersion("v0", "retired", b"0" * 32),
    }
    provider = LocalTestKeyProvider(keys, current="v2")
    assert provider.current().version == "v2"
    assert provider.fetch("v1").state == "previous"
    assert provider.rotation_status() == {"current_version": "v2", "state": "ready"}
    with pytest.raises(KeyProviderError, match="unavailable"):
        provider.fetch("v0")


def test_managed_provider_fails_closed_and_audits_only_versions() -> None:
    material = {"v2": b"2" * 32}
    provider = ManagedKeyProvider(
        current_version="v2",
        fetch_material=material.get,
        revoked_versions=frozenset({"v1"}),
    )
    assert provider.current().version == "v2"
    assert provider.audit_rotation("v1", "v2") == {
        "event": "encryption_key.rotated", "from_version": "v1", "to_version": "v2"
    }
    with pytest.raises(KeyProviderError, match="revoked"):
        provider.fetch("v1")
    with pytest.raises(KeyProviderError, match="no key"):
        provider.fetch("v3")


def test_ci_and_config_define_managed_provider_contract() -> None:
    config = json.loads(Path("config/managed-key-provider.json").read_text())
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert config["fail_closed"] is True
    assert config["material_logged"] is False
    assert "managed-key-provider:" in workflow
    assert "test_release18_managed_key_provider.py" in workflow
