"""Provider-neutral interface for versioned encryption-key storage."""

# ruff: noqa: EM101

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_KEY_BYTES: Final = 32


@dataclass(frozen=True)
class KeyVersion:
    """Metadata and material for one key version.

    Key material is intentionally available only through ``material`` at the
    encryption boundary; status/audit methods never return it.
    """

    version: str
    state: str
    material: bytes


class KeyProviderError(RuntimeError):
    """Raised when key storage cannot safely provide an authorized key."""


class LocalTestKeyProvider:
    """In-memory provider used by unit tests and local development only."""

    def __init__(self, keys: Mapping[str, KeyVersion], current: str) -> None:
        self._keys = dict(keys)
        self._current = current
        self._validate_current()

    def _validate_current(self) -> None:
        key = self._keys.get(self._current)
        if key is None or key.state != "current":
            raise KeyProviderError("current key is unavailable")
        if len(key.material) != _KEY_BYTES:
            raise KeyProviderError("current key has invalid length")

    def current(self) -> KeyVersion:
        """Return the current key for encryption."""
        self._validate_current()
        return self._keys[self._current]

    def fetch(self, version: str) -> KeyVersion:
        """Fetch an active key version for controlled decryption."""
        key = self._keys.get(version)
        if key is None or key.state == "retired":
            raise KeyProviderError("requested key is unavailable")
        if len(key.material) != _KEY_BYTES:
            raise KeyProviderError("requested key has invalid length")
        return key

    def rotation_status(self) -> dict[str, str]:
        """Return safe rotation metadata without key material."""
        return {"current_version": self._current, "state": "ready"}


class ManagedKeyProvider:
    """Adapter for a managed provider callback.

    The callback is supplied by the deployment integration (KMS/Vault/etc.)
    and returns bytes for a requested version. The application stores only
    version/state metadata and fails closed when the provider returns nothing.
    """

    def __init__(
        self,
        *,
        current_version: str,
        fetch_material: Callable[[str], bytes | None],
        revoked_versions: frozenset[str] = frozenset(),
    ) -> None:
        self._current_version = current_version
        self._fetch_material = fetch_material
        self._revoked_versions = revoked_versions

    def current(self) -> KeyVersion:
        return self.fetch(self._current_version, state="current")

    def fetch(self, version: str, *, state: str = "previous") -> KeyVersion:
        if version in self._revoked_versions:
            raise KeyProviderError("key version is revoked")
        material = self._fetch_material(version)
        if material is None:
            raise KeyProviderError("managed key provider returned no key")
        if len(material) != _KEY_BYTES:
            raise KeyProviderError("managed key has invalid length")
        return KeyVersion(version=version, state=state, material=material)

    def rotation_status(self) -> dict[str, str]:
        return {"current_version": self._current_version, "state": "managed"}

    def audit_rotation(
        self, from_version: str, to_version: str
    ) -> dict[str, str]:
        return {
            "event": "encryption_key.rotated",
            "from_version": from_version,
            "to_version": to_version,
        }
