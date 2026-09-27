"""Resolver tests: a deploy target is read back from the provider, never committed.

Runs the real script against a fixture payload (no network, no token), so the
failure modes the workflows depend on stay typed: a missing application, an
ambiguous name, and an unresolvable route must be loud instead of quietly
deploying to whatever used to be there.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOLVER = PROJECT_ROOT / "scripts" / "resolve-coolify-app.sh"
PRODUCTION_UUID = "gavhbmfdg1xiy47ewadkcnfa"
UUID_HOST = "https://gavhbmfdg1xiy47ewadkcnfa.7rb.nl"


def application(
    name: str,
    uuid: str,
    *,
    fqdn: str | None = None,
    domains: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "name": name,
        "uuid": uuid,
        "status": "running:healthy",
    }
    if fqdn is not None:
        record["fqdn"] = fqdn
    if domains is not None:
        # Coolify returns this column as a JSON string.
        record["docker_compose_domains"] = json.dumps(domains)
    return record


def run_resolver(
    tmp_path: Path, apps: object, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    fixture = tmp_path / "applications.json"
    fixture.write_text(json.dumps(apps), encoding="utf-8")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "COOLIFY_APPLICATIONS_FILE": str(fixture),
    }
    if env:
        environment.update(env)
    return subprocess.run(
        ["sh", str(RESOLVER), *args],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_compose_service_domain_is_the_route_that_is_emitted(
    tmp_path: Path,
) -> None:
    """finance-sync publishes through its `app` service, not through `web`."""
    apps = [
        application(
            "finance-sync-production",
            PRODUCTION_UUID,
            fqdn=UUID_HOST,
            domains={"app": {"domain": "https://prod-finance-sync.7rb.nl"}},
        )
    ]

    result = run_resolver(
        tmp_path,
        apps,
        "--name",
        "finance-sync-production",
        "--prefix",
        "PROD_APP",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"PROD_APP_UUID={PRODUCTION_UUID}",
        "PROD_APP_DOMAIN=https://prod-finance-sync.7rb.nl",
    ]


def test_web_service_wins_and_static_applications_use_their_fqdn(
    tmp_path: Path,
) -> None:
    compose = [
        application(
            "finance-sync-development",
            "m3jotzz6d1bj0l7ca4v2lmiz",
            fqdn="https://m3jotzz6d1bj0l7ca4v2lmiz.7rb.nl",
            domains={
                "app": {"domain": "https://ignored.example"},
                "web": {"domain": "https://finance-sync.7rb.nl"},
            },
        )
    ]
    static = [
        application(
            "financesync-test",
            "e1iq0lk2wmrdm3dadghhmtlw",
            fqdn="https://financesync.7rb.nl",
        )
    ]

    web = run_resolver(tmp_path, compose, "--name", "finance-sync-development")
    plain = run_resolver(tmp_path, static, "--name", "financesync-test")

    assert "COOLIFY_APP_DOMAIN=https://finance-sync.7rb.nl" in web.stdout
    assert "COOLIFY_APP_DOMAIN=https://financesync.7rb.nl" in plain.stdout


def test_domain_is_normalised_to_a_scheme_prefixed_host(tmp_path: Path) -> None:
    apps = [
        application(
            "finance-sync-production",
            PRODUCTION_UUID,
            fqdn=UUID_HOST,
            domains={"app": {"domain": "Finance-Sync.7RB.nl/"}},
        )
    ]

    result = run_resolver(tmp_path, apps, "--name", "finance-sync-production")

    assert "COOLIFY_APP_DOMAIN=https://finance-sync.7rb.nl" in result.stdout


def test_missing_application_is_a_typed_failure(tmp_path: Path) -> None:
    """The exact case that broke every release path: a committed UUID of an app that no longer exists."""
    apps = [
        application(
            "finance-sync-development",
            "m3jotzz6d1bj0l7ca4v2lmiz",
            fqdn=UUID_HOST,
        )
    ]

    result = run_resolver(tmp_path, apps, "--name", "finance-sync-staging")

    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == "error:coolify_application_not_found:finance-sync-staging"
    )
    assert result.stdout == ""


def test_ambiguous_name_is_a_typed_failure(tmp_path: Path) -> None:
    apps = [
        application(
            "finance-sync-production",
            "aaaaaaaaaaaaaaaaaaaaaaaa",
            fqdn=UUID_HOST,
        ),
        application(
            "finance-sync-production",
            "bbbbbbbbbbbbbbbbbbbbbbbb",
            fqdn=UUID_HOST,
        ),
    ]

    result = run_resolver(tmp_path, apps, "--name", "finance-sync-production")

    assert result.returncode == 1
    assert result.stderr.startswith(
        "error:coolify_application_ambiguous:finance-sync-production:"
    )
    assert (
        "aaaaaaaaaaaaaaaaaaaaaaaa" in result.stderr
        and "bbbbbbbbbbbbbbbbbbbbbbbb" in result.stderr
    )


def test_application_without_a_route_is_a_typed_failure(tmp_path: Path) -> None:
    apps = [
        application(
            "finance-sync-production",
            PRODUCTION_UUID,
            domains={"app": {"domain": ""}},
        )
    ]

    result = run_resolver(tmp_path, apps, "--name", "finance-sync-production")

    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == "error:coolify_domain_unresolved:finance-sync-production"
    )


def test_unparsable_payload_is_a_typed_failure(tmp_path: Path) -> None:
    fixture = tmp_path / "applications.json"
    fixture.write_text("{not json", encoding="utf-8")
    result = subprocess.run(
        ["sh", str(RESOLVER), "--name", "finance-sync-production"],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "COOLIFY_APPLICATIONS_FILE": str(fixture),
        },
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.strip() == "error:coolify_applications_unparsable"


def test_wrapped_payload_shape_is_accepted(tmp_path: Path) -> None:
    """A provider/API version bump may wrap the list; that must not read as empty."""
    apps = {
        "applications": [
            application(
                "finance-sync-production",
                PRODUCTION_UUID,
                fqdn=UUID_HOST,
                domains={"app": {"domain": "https://prod-finance-sync.7rb.nl"}},
            )
        ]
    }

    result = run_resolver(tmp_path, apps, "--name", "finance-sync-production")

    assert result.returncode == 0, result.stderr
    assert f"COOLIFY_APP_UUID={PRODUCTION_UUID}" in result.stdout


def test_name_is_required(tmp_path: Path) -> None:
    result = run_resolver(tmp_path, [], "--prefix", "PROD_APP")

    assert result.returncode == 2
    assert result.stderr.strip() == "error:application_name_required"


def test_missing_token_is_typed_and_never_reads_the_provider(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ["sh", str(RESOLVER), "--name", "finance-sync-production"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "error:coolify_api_token_missing"


def test_unknown_argument_is_rejected(tmp_path: Path) -> None:
    result = run_resolver(tmp_path, [], "--uuid", PRODUCTION_UUID)

    assert result.returncode == 2
    assert result.stderr.strip() == "error:unknown_argument:--uuid"
