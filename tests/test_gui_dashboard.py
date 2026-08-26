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
    assert "Importers" in html


def test_dashboard_exposes_control_plane_overview(client: TestClient) -> None:
    """Phase 3: the landing page is an operational control plane."""
    html = _dashboard_html(client)
    assert "control-plane/overview" in html
    for element_id in (
        "control-status",
        "control-issues",
        "control-connections",
        "control-syncs",
        "control-quality",
        "control-destinations",
    ):
        assert f'id="{element_id}"' in html
    assert "function renderControlPlane" in html
    assert "function runControlAction" in html


def test_dashboard_exposes_dedicated_data_health_page(
    client: TestClient,
) -> None:
    html = _dashboard_html(client)
    assert 'data-section="data-health"' in html
    assert 'href="#data-health"' in html
    assert 'id="section-data-health"' in html
    assert "control-plane/data-health" in html
    assert "function loadDataHealth" in html
    assert "function renderDataHealthError" in html
    assert "window.location.hash.slice(1)" in html
    assert "function renderDataHealth" in html
    assert "Elke melding heeft één concrete vervolgstap." in html


def test_dashboard_control_plane_has_safe_recovery_paths(
    client: TestClient,
) -> None:
    """Issue and sync recovery actions use existing API contracts safely."""
    html = _dashboard_html(client)
    assert "sync-runs/${encodeURIComponent(runId)}/retry" in html
    assert "escapeHtml(issue.description)" in html
    assert 'aria-live="polite"' in html
    assert "Data gedeeltelijk beschikbaar" in html


def test_dashboard_control_plane_renders_operational_action_catalog(
    client: TestClient,
) -> None:
    """Phase 5: cards expose backend-approved actions and safe states."""
    html = _dashboard_html(client)
    assert "function renderControlActions" in html
    assert "renderControlActions(c.actions)" in html
    assert "renderControlActions(d.actions)" in html
    assert (
        "data-disabled-reason" not in html
    )  # reasons are rendered as text, not executable markup
    assert "disabled-reason" in html
    assert "button.dataset.busy = 'true'" in html
    assert "setAttribute('aria-busy', 'true')" in html
    assert "control-plane/data-quality" in html
    assert "findings_total" in html


def test_dashboard_control_plane_normalizes_api_action_paths(
    client: TestClient,
) -> None:
    """Action paths may be absolute API paths from the backend contract."""
    html = _dashboard_html(client)
    assert "path.startsWith(API_BASE)" in html
    assert "Open de security-mappingflow" in html


def test_dashboard_ships_connector_list_surface(client: TestClient) -> None:
    """The page has a container the wizard renders into, plus an initial
    loading state so the user never sees a blank body."""
    html = _dashboard_html(client)
    assert 'id="connector-list"' in html
    assert 'aria-busy="true"' in html
    assert "loadConnectors()" in html


def test_dashboard_ships_read_only_database_viewer(client: TestClient) -> None:
    """The viewer exposes tenant-scoped read data without write controls."""
    html = _dashboard_html(client)
    assert 'data-section="viewer"' in html
    assert 'id="section-viewer"' in html
    assert 'id="viewer-summary"' in html
    assert 'id="viewer-accounts"' in html
    assert 'id="viewer-holdings"' in html
    assert 'id="viewer-transactions"' in html
    assert "api('GET', '/accounts?limit=200')" in html
    assert "api('GET', '/portfolio')" in html
    assert "api('GET', '/holdings?limit=500')" in html
    assert "api('GET', '/transactions?limit=50&sort_order=desc')" in html
    assert "function loadViewer()" in html
    assert "function renderViewerAccounts" in html
    assert "function renderViewerHoldings" in html
    assert "function renderViewerTransactions" in html


def test_dashboard_separates_manual_uploads_from_api_connectors(
    client: TestClient,
) -> None:
    """Manual DEGIRO files have a dedicated upload page, not an API card."""
    html = _dashboard_html(client)
    assert 'data-section="uploads"' in html
    assert 'id="section-uploads"' in html
    assert 'id="degiro-upload-files"' in html
    assert 'id="degiro-upload-connection"' in html
    assert "previewDegiroUpload()" in html
    assert "confirmDegiroUpload()" in html
    # The API connector page filters the manual-only provider client-side.
    assert "connectorCatalog.filter(c => c.name !== 'degiro_pension')" in html
    assert "c.provider_type !== 'degiro_pension'" in html


def test_dashboard_serves_login_and_register(client: TestClient) -> None:
    for path in ("/login", "/register"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "<html" in resp.text.lower()

    login = client.get("/login").text
    assert "dashboardReturnPath" in login
    assert "!requested.startsWith('//')" in login


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
        or "Could not load importers" in html
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
    assert "Importers</h1>" in html


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


# ══════════════════════════════════════════════════════════════════
# Multi-connection control panel (t_0de46e87)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_ships_add_connection_wizard(client: TestClient) -> None:
    """AC: the user can create a brand-new bunq or Trading212 connection
    next to existing ones — the page ships a wizard entry point with a
    provider selector, a label field and per-provider credential fields."""
    html = _dashboard_html(client)
    assert "Add connection" in html
    assert "openCreateWizard" in html
    assert 'id="config-provider"' in html
    assert "onWizardProviderChange" in html
    assert 'id="config-desc"' in html


def test_dashboard_renders_connections_per_provider_group(
    client: TestClient,
) -> None:
    """AC: the list renders every connection independently and groups them
    by provider, so two bunq connections show up side by side instead of
    overwriting each other."""
    html = _dashboard_html(client)
    assert "renderConnections()" in html
    assert "renderConnectionCard" in html
    assert "byProvider" in html
    assert "data-connection=" in html
    assert (
        "${conns.length} connection" in html
    )  # "(N connection(s))" group header


def test_dashboard_connection_card_shows_status_fields(
    client: TestClient,
) -> None:
    """AC: per connection the UI shows provider, label, status, selected
    account count, last attempt, last successful sync and the last error
    in cleaned form."""
    html = _dashboard_html(client)
    assert "Last attempt" in html
    assert "Last success" in html
    assert "Accounts:" in html
    assert "All accounts" in html
    assert "Paused" in html
    assert "Resume" in html
    assert "Not configured" in html
    # Sanitised last-error surface with the full message only as tooltip
    assert "conn-error" in html
    assert "cfg.last_error" in html


def test_dashboard_never_renders_credentials(client: TestClient) -> None:
    """AC: credentials are never rendered back — stored credentials are
    shown as a masked hint and edit-mode password fields use a masked
    placeholder that keeps the stored value when left blank."""
    html = _dashboard_html(client)
    assert "Credentials stored" in html
    assert "leave blank to keep the stored credentials" in html
    assert "Stored credentials are never shown" in html
    # No decrypted value or ciphertext surface in the page.
    assert "encrypted_payload" not in html


def test_dashboard_ships_per_connection_manual_sync(
    client: TestClient,
) -> None:
    """AC: a manual sync action targets a single connection via the
    per-connection manual sync API."""
    html = _dashboard_html(client)
    assert "syncConnection(" in html
    assert "Sync now" in html
    assert "api('POST', `/sync/connections/" in html


def test_dashboard_ships_connection_lifecycle_actions(
    client: TestClient,
) -> None:
    """AC: rename, pause, resume and delete are available per connection."""
    html = _dashboard_html(client)
    assert "openRenameModal(" in html
    assert "renameConnection(" in html
    assert "pauseConnection(" in html
    assert "resumeConnection(" in html
    assert "deleteConfig(" in html
    assert "api('POST', `/connectors/configs/${connectionId}/pause`)" in html
    assert "api('POST', `/connectors/configs/${connectionId}/resume`)" in html
    assert "/connectors/${encodeURIComponent(c.id || c.connection_id)}/health" in html
    assert "Provider health" in html
    assert "Reauthentication required" in html


def test_dashboard_edit_wizard_targets_connection_id(
    client: TestClient,
) -> None:
    """AC: editing updates the exact connection (multiple connections per
    provider) instead of looking up a single config per provider."""
    html = _dashboard_html(client)
    assert "openConfigModal('${provider}', '${connectionId}')" in html
    assert "api('PUT', `/connectors/configs/${connectionId}`" in html
    assert "findConfig(connectionId)" in html


def test_dashboard_account_selection_keeps_history_by_default(
    client: TestClient,
) -> None:
    """AC: changing the account selection never automatically removes
    already-imported history — the UI only purges after an explicit
    confirmation dialog, and the default save sends purge_unselected: false."""
    html = _dashboard_html(client)
    assert (
        "Changing the account selection never removes already-imported history"
        in html
    )
    assert "purge_unselected: false" in html
    assert "Remove locally stored history for deselected accounts" in html
    assert "confirm('Remove the locally stored history" in html
    assert "hasDeselected" in html


def test_dashboard_test_result_drives_account_selection(
    client: TestClient,
) -> None:
    """AC: after a successful connection test the returned account list is
    offered for selection, both on the card and inside the create wizard."""
    html = _dashboard_html(client)
    assert "openAccountsModal(" in html
    assert "Select accounts" in html
    assert "testWizardConnection" in html
    assert "renderAccountCheckboxes" in html
    assert "wizard-account-cb" in html
    assert "/test`" in html  # inline test + saved-config test paths
    assert "account_ids" in html


# ══════════════════════════════════════════════════════════════════
# Household sharing UI (t_a4ba9d6a)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_ships_household_section(client: TestClient) -> None:
    """The retired household-sharing interface is no longer shipped."""
    html = _dashboard_html(client)
    # The household-table class name is retained for table styling, but
    # the household management surface (invitations, members, sharing,
    # claim) is fully removed.
    assert "inviteMember(" not in html
    assert "/household/invitations" not in html
    assert "claimAccount(" not in html
    assert "openSharePreview(" not in html
    assert "loadHousehold(" not in html
    assert "/household/members" not in html


# ══════════════════════════════════════════════════════════════════
# Sync-schedule planning UI (t_5a064a23)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_ships_planning_section(client: TestClient) -> None:
    """The Sync Runs page ships the Planning section with Ingestion /
    Export tabs and per-row schedule management actions."""
    html = _dashboard_html(client)
    assert 'id="sync-schedules"' in html
    assert "loadSyncRuns()" in html
    assert "api('GET', '/sync-schedules?limit=500')" in html
    assert "openScheduleEditor(" in html
    assert "toggleSchedule(" in html


def test_dashboard_schedule_editor_has_live_server_preview(
    client: TestClient,
) -> None:
    """The editor's live preview is server-computed: the proposed values
    are POSTed to /sync-schedules/preview (the same pure function the
    worker uses), never approximated client-side."""
    html = _dashboard_html(client)
    assert "api('POST', '/sync-schedules/preview'" in html
    assert "schedule: vals.schedule" in html
    assert "timezone: vals.timezone" in html
    # The old client-side approximation is gone.
    assert "localPreviewHtml" not in html
    assert "Volgende momenten:" in html


def test_dashboard_schedule_editor_covers_frequencies(
    client: TestClient,
) -> None:
    """The editor supports every workday, every day, weekly with selected
    days and every N hours, showing only the fields that apply."""
    html = _dashboard_html(client)
    for option in (
        "Elke werkdag (ma",
        "Elke dag",
        "Wekelijks (bepaalde dagen)",
        "Elke N uur",
    ):
        assert option in html
    assert "sched-weekdays-block" in html
    assert "sched-interval-block" in html
    assert "sched-time-block" in html


def test_dashboard_schedule_editor_explains_disabling(
    client: TestClient,
) -> None:
    """Disabling a schedule stops scheduled runs but manual runs stay
    available — explained in plain language in the UI."""
    html = _dashboard_html(client)
    assert "handmatig uitvoeren blijft mogelijk" in html
    assert "Uitgeschakeld" in html


# ══════════════════════════════════════════════════════════════════
# Run history filters, status overview & pagination (t_eb085c3d)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_run_history_ships_status_overview(
    client: TestClient,
) -> None:
    """The run history renders a status overview (from the API's
    status_counts) with clickable chips that filter the list."""
    html = _dashboard_html(client)
    assert "run-status-overview" in html
    assert "run-status-chip" in html
    assert "setRunStatusFilter(" in html
    assert "aria-pressed" in html


def test_dashboard_run_history_ships_filters(client: TestClient) -> None:
    """Connector + status filters are wired to the /sync-runs query and
    reset, preserving pagination state."""
    html = _dashboard_html(client)
    assert "run-filter-connector" in html
    assert "run-filter-status" in html
    assert "setRunConnectorFilter(" in html
    assert "resetRunFilters()" in html
    assert "syncRunQueryString()" in html
    assert "connector" in html and "status" in html
    assert "Alle importers" in html
    assert "Alle statussen" in html


def test_dashboard_run_history_ships_pagination(client: TestClient) -> None:
    """The run history has paging controls honouring the API's
    total/limit/offset contract."""
    html = _dashboard_html(client)
    assert "run-pagination" in html
    assert "setRunPage(" in html
    assert "syncRunState.offset" in html
    assert "syncRunState.limit" in html
    assert "Vorige" in html and "Volgende" in html


def test_dashboard_run_history_status_labels_are_readable(
    client: TestClient,
) -> None:
    """Run statuses render as Dutch labels (Bezig/Voltooid/Mislukt/
    Geannuleerd), not raw enum values."""
    html = _dashboard_html(client)
    for label in ("Bezig", "Voltooid", "Mislukt", "Geannuleerd"):
        assert label in html


def test_dashboard_run_history_visible_text_states(client: TestClient) -> None:
    """Loading, saving and error states for the planning + run history
    use visible text, not colour or toast only."""
    html = _dashboard_html(client)
    assert "Preview berekenen" in html  # live preview loading
    assert "Opslaan…" in html  # editor save in progress
    assert "Uitschakelen…" in html  # toggle in progress
    assert "Inschakelen…" in html
    assert "Wijzigen mislukt:" in html  # toggle failure as text
    assert "schedule-toggle-status" in html  # inline toggle status cell
    assert 'aria-live="polite"' in html


def test_dashboard_run_history_escape_html(client: TestClient) -> None:
    """Run-history cells escape API values (connector, error message,
    datetimes) before interpolation."""
    html = _dashboard_html(client)
    assert "escapeHtml(r.connector || '')" in html
    assert "escapeHtml(String(r.error_message)" in html
    assert "escapeHtml(fmtDate(r.started_at))" in html


# ══════════════════════════════════════════════════════════════════
# Destination wizard UI (t_datalake_first_wizard)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_nav_renamed_exporters(client: TestClient) -> None:
    """The nav item and management section are labelled Exporters."""
    html = _dashboard_html(client)
    assert "> Exporters" in html
    assert 'data-section="exporters"' in html  # section id unchanged internally
    assert "Exporters</h1>" in html


def test_dashboard_destination_empty_state_explains_datalake(
    client: TestClient,
) -> None:
    """AC: the empty state explains the datalake works without an app and
    offers a clear way to add one."""
    html = _dashboard_html(client)
    assert "Je datalake blijft volledig bruikbaar" in html
    assert "Exporter toevoegen" in html
    assert "openDestinationWizard(" in html


def test_dashboard_ships_four_step_wizard(client: TestClient) -> None:
    """AC: the wizard is four steps with explicit labels and progress."""
    html = _dashboard_html(client)
    assert "Stap ${state.step} van 4" in html
    for label in ("Kies exporter", "Verbind", "Kies data", "Activeer"):
        assert label in html
    assert "destinationWizardNext(" in html
    assert "destinationWizardBack(" in html


def test_dashboard_wizard_supports_all_exporters(client: TestClient) -> None:
    """Step 1 offers every configured exporter, including read-only Jupyter."""
    html = _dashboard_html(client)
    assert "Wealthfolio" in html
    assert "Actual Budget" in html
    assert "Jupyter" in html
    assert "Firefly III" in html
    assert "Ghostfolio" in html
    assert "InvestBrain" in html
    assert "destinationWizardChoose(" in html


def test_dashboard_wizard_connect_steps_ship_required_controls(
    client: TestClient,
) -> None:
    """AC: step 2 renders the server URL, credential (masked), a budget
    discovery action and a test-connection action where relevant."""
    html = _dashboard_html(client)
    assert "dest-url" in html
    assert "dest-secret" in html
    assert 'type="password"' in html
    assert "Budgetten ontdekken" in html
    assert "destinationWizardDiscoverBudgets(" in html
    assert "destinationWizardTest()" in html
    assert "Verbinding testen" in html
    # Credentials are never rendered back: leaving the password blank keeps
    # the existing encrypted secret.
    assert "Ongewijzigd laten om bestaand geheim te behouden" in html


def test_dashboard_wizard_activates_without_double_exports(
    client: TestClient,
) -> None:
    """AC: activation writes through the destinations API (not the retired
    exporters surface) and Jupyter shows its one-time key + notebook."""
    html = _dashboard_html(client)
    assert "api('POST', '/destinations'" in html
    assert "api('POST', `/destinations/${saved.id}/activate`)" in html
    assert "jupyter_bootstrap" in html
    assert "FINANCE_SYNC_JUPYTER_TOKEN" in html
    assert "starter-notebook" in html


def test_dashboard_destination_cards_expose_status_and_actions(
    client: TestClient,
) -> None:
    """AC: each destination card shows status, last/next run and the
    pause/delete/manual-sync actions."""
    html = _dashboard_html(client)
    assert "Laatste run:" in html
    assert "Volgende run:" in html
    assert "runDestination(" in html
    assert "deleteDestination(" in html
    assert (
        "destinationAction(" in html
    )  # pause etc. via POST /destinations/{id}/{action}
    assert "Notebook downloaden" in html  # Jupyter download action
    assert "Sleutel roteren" in html


# ══════════════════════════════════════════════════════════════════
# Holding feed & calendar UI (t_76f1de63)
# ══════════════════════════════════════════════════════════════════


def test_dashboard_ships_holding_news_section(client: TestClient) -> None:
    """AC: the control panel ships a Holdingnieuws section with a feed
    container and a calendar container."""
    html = _dashboard_html(client)
    assert 'data-section="holding-news"' in html
    assert "Holdingnieuws" in html
    assert 'id="section-holding-news"' in html
    assert 'id="holding-feed"' in html
    assert 'id="holding-calendar"' in html


def test_dashboard_holding_news_section_is_permission_gated(
    client: TestClient,
) -> None:
    """AC: the nav entry is gated behind market-intelligence:read — the
    same permission the holding-relevance API endpoints require."""
    html = _dashboard_html(client)
    assert 'data-perm="market-intelligence:read"' in html
    assert "applySectionPermissions" in html


def test_dashboard_holding_news_consumes_feed_api(client: TestClient) -> None:
    """AC: the feed renders from the /holding-relevance API with the
    ranked/clustered contract (items + total)."""
    html = _dashboard_html(client)
    assert "loadHoldingNews()" in html
    assert "holding-relevance/feed" in html
    assert "holding-relevance/calendar" in html
    assert "holdingFeedItems" in html
    assert "holdingState.total" in html


def test_dashboard_holding_news_ships_filters(client: TestClient) -> None:
    """AC: filters for security, account, item type, date and
    unread/acknowledged state are wired to the API query string."""
    html = _dashboard_html(client)
    assert "holding-filter-security" in html
    assert "holding-filter-account" in html
    assert "holding-filter-type" in html
    assert "holding-filter-ack" in html
    assert "holding-filter-from" in html
    assert "holding-filter-to" in html
    assert "setHoldingFilter(" in html
    assert "resetHoldingFilters()" in html
    assert "holdingFeedQueryString()" in html
    # The ack state maps to the API's unread_only/acknowledged params.
    assert "unread_only" in html
    assert "acknowledged" in html
    assert "Ongelezen" in html
    assert "Gelezen" in html


def test_dashboard_holding_news_ships_ack_flow(client: TestClient) -> None:
    """AC: acknowledged/unread state changes are reflected in the UI —
    per-cluster ack buttons POST to the ack endpoint and re-render."""
    html = _dashboard_html(client)
    assert "ackCluster(" in html
    assert "holding-relevance/clusters/" in html
    assert "/ack" in html
    assert "Markeer gelezen" in html
    assert "Markeer ongelezen" in html
    assert "item.acknowledged" in html


def test_dashboard_holding_news_ships_calendar(client: TestClient) -> None:
    """AC: the calendar renders upcoming event clusters by date."""
    html = _dashboard_html(client)
    assert "renderHoldingCalendar()" in html
    assert "Eventkalender" in html
    assert "holdingCalendarEvents" in html
    assert "event_date" in html


def test_dashboard_holding_news_escapes_api_values(client: TestClient) -> None:
    """AC: headlines, security names and source URLs from the API are
    escaped before interpolation (no XSS via syndicated titles)."""
    html = _dashboard_html(client)
    assert "escapeHtml(item.headline" in html
    assert "escapeHtml(item.security_ticker" in html
    assert "escapeHtml(item.cluster_id" in html
    assert "escapeHtml(s.url)" in html
    assert 'rel="noopener noreferrer"' in html


def test_dashboard_holding_news_fallback_options_from_tenant_endpoints(
    client: TestClient,
) -> None:
    """AC: filter options are derived from tenant-scoped holdings and
    accounts endpoints so a user only ever sees their own data."""
    html = _dashboard_html(client)
    assert "api('GET', '/holdings?limit=500')" in html
    assert "api('GET', '/accounts?limit=200')" in html
    assert "holdingSecurities" in html
    assert "holdingAccounts" in html


def test_dashboard_holding_news_graceful_error(client: TestClient) -> None:
    """AC: a failing feed call renders a friendly inline error with a
    Retry action, never a blank body."""
    html = _dashboard_html(client)
    assert "Could not load holding feed" in html
    assert "renderInlineError('holding-feed'" in html
    assert (
        "renderInlineError('holding-calendar'" in html
        or "loadHoldingNews()" in html
    )


# ══════════════════════════════════════════════════════════════════
# Holding-relevance companion view (t_76f1de63)
# ══════════════════════════════════════════════════════════════════


def test_companion_view_is_served(client: TestClient) -> None:
    """AC: the documented Wealthfolio companion page is server-rendered
    at /holdings-relevance."""
    resp = client.get("/holdings-relevance")
    assert resp.status_code == 200
    html = resp.text
    assert "Holdingnieuws" in html
    assert "companion view" in html
    assert "holding-relevance/feed" in html


def test_companion_view_never_touches_wealthfolio_db(
    client: TestClient,
) -> None:
    """AC: the companion page consumes only the finance-sync API — there
    is no SQLite / Wealthfolio database access anywhere in the page."""
    html = client.get("/holdings-relevance").text
    assert "holding-relevance/feed" in html
    assert "holding-relevance/calendar" in html
    # No direct DB file access, no sqlite identifiers in the page.
    assert "sqlite" not in html.lower()
    assert "wealthfolio.db" not in html.lower()
    assert "INSERT INTO" not in html


def test_companion_view_escapes_and_is_lockscreen_safe(
    client: TestClient,
) -> None:
    """AC: the companion page escapes API values and never renders
    position sizes or financial values."""
    html = client.get("/holdings-relevance").text
    assert "escapeHtml(item.headline" in html
    assert "escapeHtml(item.security_ticker" in html
    assert 'rel="noopener noreferrer"' in html
    # Lockscreen-safe: no financial value / position-size rendering.
    assert "market_value" not in html
    assert "position_size" not in html.lower()


def test_companion_view_supports_embedded_token_fragment(
    client: TestClient,
) -> None:
    """AC: the embed path reads an optional #token= fragment instead of
    leaking the JWT into a query string."""
    html = client.get("/holdings-relevance").text
    assert "window.location.hash" in html
    assert "frag.get('token')" in html
    assert "localStorage.getItem('fs_token')" in html
    # The token is read from the fragment, never written to the query.
    assert "location.search" not in html
