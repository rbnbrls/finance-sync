"""Contract tests for the production/staging Coolify deployment targets.

The deployment targets must be resolved from the provider at run time. A
committed application UUID is the defect this file exists to prevent: Coolify
mints a new UUID on every recreation, so the old one keeps the workflow green
looking while it deploys to a deleted application (HTTP 404, issue #430).
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = PROJECT_ROOT / ".github" / "workflows" / "deploy.yml"
RELEASE = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
RESOLVER = "scripts/resolve-coolify-app.sh"

# UUIDs Coolify has minted for this repository so far: the deleted production
# app, an even older one, and the staging app whose deploy step still 404s.
HISTORICAL_APP_UUIDS = (
    "xnenwsyifmv81xkvdxkbkx3a",
    "5iov9i8w9mazc6vhismo3enx",
    "mdeal4aqq9ycnozn3mg83zix",
)
COOLIFY_UUID = re.compile(r"\b[a-z0-9]{24}\b")


def test_deploy_workflow_resolves_the_production_application_by_name() -> None:
    workflow = DEPLOY.read_text(encoding="utf-8")

    for uuid in HISTORICAL_APP_UUIDS:
        assert uuid not in workflow
    assert not COOLIFY_UUID.findall(workflow), (
        "no Coolify UUID may be committed"
    )
    assert RESOLVER in workflow
    assert workflow.count(f"RESOLVED=$(./{RESOLVER}") == 1
    assert "--name" in workflow and "--prefix COOLIFY_APP" in workflow
    # The resolved identity is what the deploy call and the health gate use.
    assert r"\"uuid\": \"${COOLIFY_APP_UUID}\"" in workflow
    assert '"${COOLIFY_APP_DOMAIN}/health/live"' in workflow
    assert '"${COOLIFY_APP_DOMAIN}/health/ready"' in workflow
    # A missing target must fail closed before the provider is called.
    assert "resolve-coolify-app.sh" in workflow


def test_release_workflow_resolves_both_applications_by_name() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")

    for uuid in HISTORICAL_APP_UUIDS:
        assert uuid not in workflow
    assert not COOLIFY_UUID.findall(workflow), (
        "no Coolify UUID may be committed"
    )
    assert workflow.count(RESOLVER) == 4, (
        "staging deploy, smoke gate, promotion (and the env note)"
    )
    assert workflow.count(f"RESOLVED=$(./{RESOLVER}") == 3, (
        "staging deploy, smoke gate and promotion each resolve their target"
    )
    assert "--prefix STAGING_APP" in workflow
    assert "--prefix PROD_APP" in workflow
    assert r"\"uuid\": \"${STAGING_APP_UUID}\"" in workflow
    assert r"\"uuid\": \"${PROD_APP_UUID}\"" in workflow
    # The smoke gate must use the route Coolify serves, not the generated
    # <uuid>.7rb.nl hostname a Compose application does not route.
    assert 'SMOKE_BASE_URL="${STAGING_APP_DOMAIN}"' in workflow
    assert (
        '.7rb.nl"'
        not in workflow.split("SMOKE_BASE_URL", 1)[1].split("\n", 1)[0]
    )


def test_target_names_come_from_the_repository_not_from_a_literal() -> None:
    """Renaming an application must not need a code change either."""
    for path, prefix in (
        (DEPLOY, "COOLIFY_APP_NAME_PRODUCTION"),
        (RELEASE, "COOLIFY_APP_NAME_STAGING"),
    ):
        workflow = path.read_text(encoding="utf-8")
        assert f"${{{{ vars.{prefix} }}}}" in workflow
        assert '$(basename "${GITHUB_REPOSITORY}")-' in workflow


def test_deploy_workflow_annotates_an_unresolved_target_with_its_typed_reason() -> (
    None
):
    """A missing target must name itself instead of blaming the token.

    Issue #1005: every push to main failed on ``Trigger Coolify deployment`` with
    the provider's bare ``404 {"message":"No resources found."}``, which reads
    like a credential problem. The resolve step now surfaces the resolver's typed
    reason as a run annotation naming the application and the plane.
    """
    workflow = DEPLOY.read_text(encoding="utf-8")

    assert (
        "::error title=Coolify deploy target unresolved::${REASON}" in workflow
    )
    annotation = workflow.split(
        "::error title=Coolify deploy target unresolved::", 1
    )[1].split("\n", 1)[0]
    assert "coolify_application_not_found" in annotation
    assert "recreate it in Coolify" in annotation
    assert "COOLIFY_APP_NAME_PRODUCTION" in annotation


def test_resolver_is_executable_and_documented() -> None:
    resolver = PROJECT_ROOT / RESOLVER

    assert resolver.is_file()
    assert resolver.stat().st_mode & 0o111, "workflows invoke it directly"
    text = resolver.read_text(encoding="utf-8")
    for reason in (
        "error:coolify_application_not_found",
        "error:coolify_application_ambiguous",
        "error:coolify_domain_unresolved",
    ):
        assert reason in text
