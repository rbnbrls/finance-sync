# Versioning and Compatibility Policy

## Package version: `finance-sync-sdk`

This document defines how the SDK is versioned and what compatibility
guarantees third-party plugin authors can rely on.

---

## 1. Semantic versioning

`finance-sync-sdk` follows [PEP 440](https://peps.python.org/pep-0440/)
with [Semantic Versioning 2.0](https://semver.org/) semantics:

```
MAJOR.MINOR.PATCH
```

| Bump | When | Example |
|------|------|---------|
| **MAJOR** | Breaking changes to the public API or plugin interface contract | `0.1.0` → `1.0.0` |
| **MINOR** | New features added in a backward-compatible way | `0.1.0` → `0.2.0` |
| **PATCH** | Bug fixes, security patches, documentation improvements | `0.1.0` → `0.1.1` |

### Current phase: initial development

While the SDK is in `0.x` phase (major version `0`), **minor version bumps
may include breaking changes**. This follows the SemVer specification:

> "Major version zero (0.y.z) is for initial development. Anything may
> change at any time. The public API should not be considered stable."

Plugin authors should pin their dependency with an upper bound:

```toml
dependencies = [
    "finance-sync-sdk>=0.1.0,<0.2.0",
]
```

Once the SDK reaches `1.0.0`, breaking changes will only occur on major
version bumps, and the following policy applies.

---

## 2. Compatibility policy (≥1.0.0)

### 2.1 Public API guarantee

The **public API** includes everything explicitly exported from the
top-level `finance_sync_sdk` package and its documented sub-modules:

- `finance_sync_sdk` (top-level exports)
- `finance_sync_sdk.plugin` — `ConnectorPlugin`, `ExporterPlugin`
- `finance_sync_sdk.models` — all Pydantic data models
- `finance_sync_sdk.config` — `PluginConfigSchema`, `CredentialField`
- `finance_sync_sdk.credentials` — `CredentialProvider`, `EnvCredentialProvider`,
  `DictCredentialProvider`
- `finance_sync_sdk.exceptions` — all exception classes
- `finance_sync_sdk.rate_limiter` — `RateLimitPolicy`, `RateLimiter`
- `finance_sync_sdk.registry` — `PluginRegistry`

Within the same major version:

- **No** abstract method signatures will change (parameters, return types).
- **No** public class or function will be removed or renamed.
- **No** Pydantic model fields will be removed (new fields may be added
  with defaults, preserving backward compatibility).

### 2.2 What is NOT covered

The following are **explicitly excluded** from the compatibility guarantee
and may change in minor or patch releases:

- Internal/private modules and symbols prefixed with `_` (e.g.
  `_rate_limited_fetch_accounts()` implementation details).
- Transitive dependencies pinned by the SDK (plugins should declare their
  own dependency bounds).
- Test helpers in `tests/` — they are not part of the shipped package.
- Type-checking configuration (py.typed marker is provided but type
  inference improvements may change in minor releases).

### 2.3 Deprecation policy

Before a breaking change is made in a major release:

1. The old API is marked as **deprecated** in a minor release with a
   `DeprecationWarning`.
2. Deprecated symbols remain functional for at least **one minor release
   cycle** (typically 3 months).
3. The deprecation notice documents the migration path to the new API.

Deprecation notices are logged via `warnings.warn()` with
`stacklevel=2`, visible in both development and production logs.

### 2.4 Plugin contract stability

The plugin lifecycle methods (`authenticate`, `fetch_accounts`,
`fetch_transactions`, `export`, `transform_accounts`,
`transform_transactions`, `health`, `run_export`) are considered **stable
public API** starting at `1.0.0`.

New lifecycle hooks may be added as optional methods with a default
implementation (no-op or passthrough) so existing plugins continue
to work unmodified.

### 2.5 Entry-point groups

| Group | Stability | Purpose |
|-------|-----------|---------|
| `finance_sync_sdk.plugins` | Stable | Connector plugin discovery |
| `finance_sync_sdk.exporters` | Stable | Exporter plugin discovery |

These entry-point group names are stable within a major version.

---

## 3. Python version support

The SDK supports the **two most recent minor releases** of CPython 3.

| SDK version | Python versions |
|-------------|----------------|
| 0.1.x | 3.12, 3.13 |
| 1.0+ | TBD (at least 3.12+) |

Python version support is bumped in **minor** releases. Dropping support
for an older Python version is not considered a breaking change per SemVer,
but will be documented in the release notes.

---

## 4. Dependency pins

The SDK pins only direct runtime dependencies with a minimum version:

```toml
dependencies = [
    "pydantic>=2.10",
]
```

- **Patch-level pins** are never used — you always get the latest
  compatible patch of each dependency.
- **Transitive dependencies** are not pinned. If a security fix requires
  a minimum transitive version, it is documented in the release notes.

Plugin authors should independently verify their dependency tree:

```bash
pip install finance-sync-sdk
pip freeze | grep -E "^(pydantic|finance)"
```

---

## 5. Changelog and release cadence

There is no fixed release cadence. Releases are made when warranted:

| Trigger | Typical version bump |
|---------|---------------------|
| Bug fix or security patch | PATCH |
| New feature (non-breaking) | MINOR |
| Breaking API change | MAJOR |

Every GitHub release includes:

- A changelog entry in the release body.
- The associated `sdk-v*` git tag (e.g. `sdk-v0.1.1` for the
  `finance-sync-sdk` package).
- An updated `py.typed` marker if type annotations changed.
- A PyPI release (automated via `.github/workflows/publish-sdk.yml`).

---

## 6. Plugin compatibility declaration

Third-party plugins should declare their SDK compatibility range in their
`pyproject.toml`:

```toml
[project]
dependencies = [
    "finance-sync-sdk>=0.1.0,<1.0.0",
]

[project.entry-points."finance_sync_sdk.plugins"]
mybank = "mybank_finance_sync.plugin:MyBankPlugin"
```

The `PluginRegistry.describe()` method returns the plugin's declared
`plugin_version` so the host application can warn about version mismatches.

---

## 7. Backward compatibility for data models

| Model field change | Type | Compatibility |
|-------------------|------|---------------|
| New optional field added | Minor | Backward-compatible |
| New required field added | Major | Breaking |
| Field removed | Major | Breaking |
| Field type changed | Major | Breaking |
| Field default changed | Minor | Backward-compatible |

All SDK Pydantic models use `Field(default=...)` for optional fields.
New fields in a minor release always have a sensible default so existing
plugin code continues to work unmodified.

---

## 8. Version compatibility matrix

| finance-sync host | finance-sync-sdk | Notes |
|-------------------|------------------|-------|
| 0.1.x | 0.1.x | Initial development phase |
| 1.0+ | 1.0+ | Stable API, full compatibility policy active |

The host application (`finance-sync`) declares its own SDK dependency:

```toml
[project.dependencies]
"finance-sync-sdk" = ">=0.1.0,<1.0.0"
```

This ensures the host and third-party plugins always use a compatible SDK
version range.
