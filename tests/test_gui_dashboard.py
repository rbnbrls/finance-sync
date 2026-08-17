"""GUI dashboard tests — connector configuration wizard for user role.

Regression + polish coverage for issue #268 (connectors page failing for a
signed-in non-admin user).  The backend authorization fix itself is covered
by ``tests/test_connectors_auth.py`` and the integration-level regression in
``tests/integration/test_connectors_auth_pg.py``.

These tests assert the frontend contract: the dashboard template is served
without an error banner, ships a per-endpoint resilient loader with a
friendly inline Retry action, renders a discoverable configuration wizard
with clear labels and required-field validation, stays accessible
(dialog roles, aria attributes, focus handling, responsive breakpoints) and
gates admin-only navigation behind the user's permissions.
"""

# pyright: basic

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from finance_sync.app import create_app
from finance_sync.config.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import FastAPI

_TEST_SECRET: SecretStr = SecretStr("test-secret-key-at-least-16-chars")


@pytest.fixture
def app() -> FastAPI:
    """App with minimal settings (no DB/Redis) — GUI pages render fine."""
    return create_app(
        settings=Settings(
            database_url=None,
            redis_url=None,
            secret_key=_TEST_SECRET,
        )
    )


@pytest.fixture
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def _dashboard_html(client: TestClient) -> str:
    """Fetch the dashboard page and fail loudly if it errored."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "finance-sync" in html
    return html


# ── Page-level serving ────────────────────────────────────────────


def test_dashboard_is_served(client: TestClient) -> None:
    """AC1: the connector page is served for any signed-in session — the
    client side gates on the stored token; the server never 500s."""
    html = _dashboard_html(client)
    assert "section-connectors" in html
    assert "Connectors" in html


def test_dashboard_ships_connector_list_surface(client: TestClient) -> None:
    """The page has a container the wizard renders into, plus an initial
    loading state so the user never sees a blank body."""
    html = _dashboard_html(client)
    assert 'id="connector-list"' in html
    assert 'aria-busy="true"' in html
    assert "loadConnectors()" in html


def test_dashboard_serves_login_and_register(client: TestClient) -> None:
    for path in ("/login", "/register"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "<html" in resp.text.lower()


# ── Friendly inline error with Retry (AC: no blank/crashing page) ─


def test_dashboard_ships_retry_loader_helpers(client: TestClient) -> None:
    html = _dashboard_html(client)
    assert "renderInlineError" in html
    assert "function retryLoad" in html
    assert "Retry" in html
    # Each section re-loads itself, and errors never use innerHTML with a
    # raw exception — user-facing text goes through escapeHtml.
    assert "escapeHtml" in html


def test_dashboard_loads_connectors_resiliently(client: TestClient) -> None:
    """A single failing connector endpoint must not blank the whole page:
    the loader settles each API call independently and renders whatever
    loaded, then offers a Retry for the rest."""
    html = _dashboard_html(client)
    assert "Promise.allSettled" in html
    assert (
        "renderInlineError('connector-list'" in html
        or "Could not load connectors" in html
    )


def test_dashboard_inline_errors_carry_retry_actions(
    client: TestClient,
) -> None:
    """Every connector API call path exposes a friendly inline error with
    a Retry action: page load, save, test connection, DEGIRO preview and
    DEGIRO confirm."""
    html = _dashboard_html(client)
    # Page-level loader
    assert "&#8635; Retry" in html
    assert "retryLoad(" in html
    # Wizard save
    assert "Retry Save" in html
    # Test connection
    assert "Connection test failed" in html
    assert "testConnection(" in html
    # DEGIRO preview + confirm
    assert "Could not preview the import" in html
    assert "Could not confirm the import" in html
    assert "previewDegiroImport(" in html
    assert "confirmDegiroImport(" in html


def test_dashboard_saves_connect_config_with_validation(
    client: TestClient,
) -> None:
    """Wizard saves through the API and validates required fields with
    friendly inline messaging instead of raw browser tooltips."""
    html = _dashboard_html(client)
    assert "saveConfig(event" in html
    assert (
        "required fields" in html.lower()
        or "Please fill in the required fields" in html
    )
    assert "api('PUT', `/connectors/configs/" in html
    assert "api('POST', '/connectors/configs'" in html


# ── Wizard discoverability & labels ───────────────────────────────


def test_dashboard_wizard_modal_is_accessible(client: TestClient) -> None:
    """Dialog semantics, labelled form controls and a labelled close
    affordance for the configuration modal."""
    html = _dashboard_html(client)
    # Modal helper wires role=dialog / aria-modal on open.
    assert "modal.setAttribute('role', 'dialog')" in html
    assert "aria-modal" in html
    # Credential/option fields render with real <label for=...> pairs.
    assert 'label for="cred-' in html
    assert 'label for="opt-' in html
    # Required fields are surfaced for assistive tech.
    assert "aria-required" in html


def test_dashboard_wizard_escaping_prevents_xss(client: TestClient) -> None:
    """Connector names/descriptions coming from the API are HTML-escaped
    before they are interpolated into the DOM."""
    html = _dashboard_html(client)
    assert "escapeHtml(String(c.display_name || c.name))" in html
    assert "escapeHtml" in html


# ── Permission-aware navigation (AC4: admin-only stays hidden) ────


def test_dashboard_nav_has_permission_gates(client: TestClient) -> None:
    """Nav sections carry the permission they need; the page applies them
    from the signed-in user's /auth/me permissions so admin-only features
    never render for a plain user."""
    html = _dashboard_html(client)
    assert 'data-perm="connectors:read"' in html
    assert 'data-perm="sync:read"' in html
    assert "applySectionPermissions" in html
    assert "currentUser" in html


def test_dashboard_hides_permission_gated_section_logic(
    client: TestClient,
) -> None:
    """The gating logic exists client-side: sections without the required
    permission get the hidden class, and switchSection falls back to
    Connectors when the target is gated."""
    html = _dashboard_html(client)
    assert "a.classList.toggle('hidden', !allowed)" in html
    assert "link.classList.contains('hidden')" in html
    assert "name = 'connectors'" in html  # fallback target


def test_dashboard_has_no_admin_api_key_ui(client: TestClient) -> None:
    """AC4: unrelated admin-only features (API-key management) are not
    surfaced in the dashboard at all — only the API exposes them."""
    html = _dashboard_html(client)
    assert "api-keys" not in html.lower()
    assert "API Key Management" not in html


# ── Responsive & accessible (AC3) ─────────────────────────────────


def test_dashboard_has_responsive_breakpoints(client: TestClient) -> None:
    html = _dashboard_html(client)
    assert "@media (max-width: 900px)" in html
    assert "@media (max-width: 560px)" in html


def test_dashboard_has_reduced_motion_support(client: TestClient) -> None:
    html = _dashboard_html(client)
    assert "prefers-reduced-motion" in html


def test_dashboard_toast_and_regions_are_live(client: TestClient) -> None:
    """Announcements (toasts, load results) are polite-live; regions are
    labelled so screen-reader users can navigate sections by name."""
    html = _dashboard_html(client)
    assert 'aria-live="polite"' in html
    assert 'role="region"' in html
    assert 'aria-labelledby="connectors-title"' in html


def test_dashboard_has_escape_to_close_and_focus_restore(
    client: TestClient,
) -> None:
    """Modals close on Escape and restore focus to the invoking control."""
    html = _dashboard_html(client)
    assert "e.key === 'Escape'" in html
    assert "__lastFocused" in html


def test_dashboard_init_never_blank_screens(client: TestClient) -> None:
    """The boot sequence tolerates a failed /auth/me without dropping the
    page: connectors still load and no error banner is forced."""
    html = _dashboard_html(client)
    assert "Could not fetch profile" in html
    assert "await loadConnectors();" in html
