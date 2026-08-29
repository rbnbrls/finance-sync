"""CLI entry point for finance-sync.

Usage::

    python -m finance_sync reconcile --help
    python -m finance_sync reconcile
    python -m finance_sync reconcile --account-ids acct_1,acct_2 --days-back 30
"""

from __future__ import annotations

import asyncio
import sys
from argparse import (
    ArgumentParser,
    BooleanOptionalAction,
    Namespace,
    RawDescriptionHelpFormatter,
)
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine

from finance_sync.config.settings import Settings, secret_value
from finance_sync.container import Container
from finance_sync.db.uow import UnitOfWork
from finance_sync.models.enums import ReconciliationRunStatus
from finance_sync.observability.logging import configure_logging
from finance_sync.services.reconciliation import ReconciliationService

# Production Wealthfolio instances.  The CLI smoke/push commands write to
# the configured instance; these URLs require an explicit --allow-prod to
# prevent test data from polluting production again (issue #504).
_WF_PROD_BASE_URLS = {
    "http://192.168.3.50:8080",
    "https://wealthfolio.7rb.nl",
    "http://wealthfolio.7rb.nl",
}


def _build_parser() -> ArgumentParser:
    """Build the top-level argument parser."""
    parser = ArgumentParser(
        prog="finance-sync",
        description="Self-hosted, API-first financial data platform — CLI",
        formatter_class=RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── reconcile ──────────────────────────────────────────────────────
    rec = sub.add_parser(
        "reconcile",
        help="Run a reconciliation analysis and print findings",
        description=(
            "Run a full reconciliation analysis (duplicate detection, "
            "cross-connector gap detection, missing transaction detection) "
            "for the configured tenant and print a summary of findings."
        ),
    )
    rec.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account IDs to analyze (default: all)",
    )
    rec.add_argument(
        "--provider-keys",
        default=None,
        help=(
            "Comma-separated provider/connector keys to compare (default: all)"
        ),
    )
    rec.add_argument(
        "--date-from",
        default=None,
        help=(
            "Earliest transaction date in ISO-8601 format "
            "(e.g. '2026-01-01' or '2026-01-01T00:00:00Z'). "
            "Overrides --days-back."
        ),
    )
    rec.add_argument(
        "--date-to",
        default=None,
        help=(
            "Latest transaction date in ISO-8601 format "
            "(e.g. '2026-06-30' or '2026-06-30T23:59:59Z'). "
            "Overrides --days-back."
        ),
    )
    rec.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="Number of days to look back for the analysis window"
        " (default: 90 — ignored when --date-from/--date-to are set)",
    )
    rec.add_argument(
        "--threshold-hours",
        type=int,
        default=48,
        help="Max hour gap for duplicate candidates (default: 48)",
    )
    rec.add_argument(
        "--tenant-id",
        default=None,
        help="Tenant ID to reconcile (default: from settings / env)",
    )
    rec.add_argument(
        "--connector-a",
        default=None,
        help=(
            "First connector/provider key for targeted comparison "
            "(requires --connector-b)"
        ),
    )
    rec.add_argument(
        "--connector-b",
        default=None,
        help=(
            "Second connector/provider key for targeted comparison "
            "(requires --connector-a)"
        ),
    )
    rec.add_argument(
        "--detect-duplicates",
        action=BooleanOptionalAction,
        default=True,
        help="Scan for duplicate transactions (default: enabled)",
    )

    # ── compare ────────────────────────────────────────────────────────
    cmp = sub.add_parser(
        "compare",
        help="Compare two specific connectors and print findings",
        description=(
            "Run a reconciliation analysis limited to transactions from "
            "two specified provider/connector keys and print the "
            "discrepancy report."
        ),
    )
    cmp.add_argument(
        "connector_a",
        help="First connector/provider key (e.g. 'bunq')",
    )
    cmp.add_argument(
        "connector_b",
        help="Second connector/provider key (e.g. 'trading212')",
    )
    cmp.add_argument(
        "--date-from",
        default=None,
        help=(
            "Earliest transaction date in ISO-8601 format "
            "(default: 90 days ago)"
        ),
    )
    cmp.add_argument(
        "--date-to",
        default=None,
        help=("Latest transaction date in ISO-8601 format (default: now)"),
    )
    cmp.add_argument(
        "--threshold-hours",
        type=int,
        default=48,
        help="Max hour gap for duplicate candidates (default: 48)",
    )
    cmp.add_argument(
        "--tenant-id",
        default=None,
        help="Tenant ID to reconcile (default: from settings / env)",
    )
    cmp.add_argument(
        "--detect-duplicates",
        action=BooleanOptionalAction,
        default=True,
        help="Scan for duplicate transactions (default: enabled)",
    )

    # ── wealthfolio ──────────────────────────────────────────────────
    _build_wealthfolio_subparser(sub)

    # ── actual-budget ────────────────────────────────────────────────
    _build_actual_budget_subparser(sub)
    _build_securo_subparser(sub)

    # ── ghostfolio ───────────────────────────────────────────────────
    _build_ghostfolio_subparser(sub)
    _build_investbrain_subparser(sub)

    return parser


def _build_wealthfolio_subparser(
    sub: Any,
) -> ArgumentParser:
    """Build the ``wealthfolio`` subcommand parser."""
    wf = sub.add_parser(
        "wealthfolio",
        help="Export and push data to Wealthfolio",
        description=(
            "Export finance-sync data to Wealthfolio. By default writes CSV "
            "files. With --server-url and --password, pushes directly to a "
            "running Wealthfolio self-hosted instance via its REST API."
        ),
    )
    wf_sub = wf.add_subparsers(dest="wf_command", required=True)

    # ── push ─────────────────────────────────────────────────────────
    push = wf_sub.add_parser(
        "push",
        help="Push transactions to a running Wealthfolio instance",
        description=(
            "Export pending transactions from finance-sync and push them "
            "directly to a running Wealthfolio self-hosted instance via "
            "its REST API. Requires WEALTHFOLIO_SERVER_URL and "
            "WEALTHFOLIO_PASSWORD to be configured."
        ),
    )
    push.add_argument(
        "--server-url",
        default=None,
        help="Wealthfolio server URL (overrides WEALTHFOLIO_SERVER_URL env)",
    )
    push.add_argument(
        "--password",
        default=None,
        help="Wealthfolio password (overrides WEALTHFOLIO_PASSWORD env)",
    )
    push.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account IDs to push (default: all active)",
    )
    push.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="Days of transaction history to push (default: 90)",
    )
    push.add_argument(
        "--full-history",
        action="store_true",
        default=False,
        help="Backfill the complete finance-sync transaction history",
    )
    push.add_argument(
        "--rebuild",
        action="store_true",
        default=False,
        help="Delete and rebuild the selected Wealthfolio destination data",
    )
    push.add_argument(
        "--max-transactions",
        type=int,
        default=None,
        help="Hard limit on transactions to push per run",
    )
    push.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be pushed without actually pushing",
    )

    smoke = wf_sub.add_parser(
        "smoke",
        help="Run a privacy-safe idempotency smoke test",
        description=(
            "Push the selected investment accounts twice, verify that the "
            "second pass imports nothing, and check remote account/activity/"
            "holding visibility without printing financial values.  Writes "
            "to the configured Wealthfolio instance: pass --allow-prod when "
            "targeting the production instance (issue #504 guard)."
        ),
    )
    smoke.add_argument("--server-url", default=None)
    smoke.add_argument("--password", default=None)
    smoke.add_argument("--account-ids", default=None)
    smoke.add_argument("--days-back", type=int, default=3650)
    smoke.add_argument(
        "--allow-prod",
        action="store_true",
        default=False,
        help=(
            "Allow running the smoke push against the production Wealthfolio "
            "instance (LXC 104 / wealthfolio.7rb.nl).  Required because a "
            "smoke push created the corrupted test account behind issue #504."
        ),
    )

    # ── export (CSV) ─────────────────────────────────────────────────
    export = wf_sub.add_parser(
        "export",
        help="Export transactions to Wealthfolio CSV files",
        description=(
            "Export transactions and holdings from finance-sync to "
            "Wealthfolio-compatible CSV files in the configured output "
            "directory."
        ),
    )
    export.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Output directory for CSV files (overrides WEALTHFOLIO_OUTPUT_DIR)"
        ),
    )
    export.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account IDs to export (default: all active)",
    )
    export.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="Days of transaction history to export (default: 90)",
    )

    return wf


def _build_actual_budget_subparser(sub: Any) -> ArgumentParser:
    """Build the ``actual-budget`` subcommand parser."""
    ab = sub.add_parser(
        "actual-budget",
        help="Export and push data to Actual Budget",
        description=(
            "Export finance-sync data to an Actual Budget server. By "
            "default runs the full export cycle: resolves or creates "
            "Actual Budget accounts and imports pending transactions "
            "via the reconcile (dedup-aware) flow, writing a CSV "
            "summary for manual import. Requires ACTUAL_BUDGET_SERVER_URL "
            "and ACTUAL_BUDGET_PASSWORD to be configured."
        ),
    )
    ab_sub = ab.add_subparsers(dest="ab_command", required=True)

    # ── export ──────────────────────────────────────────────────────
    export = ab_sub.add_parser(
        "export",
        help="Run the Actual Budget export cycle",
        description=(
            "Run a full Actual Budget export cycle: connect to the "
            "server, resolve or create accounts, import pending "
            "transactions and write a CSV summary. Requires "
            "ACTUAL_BUDGET_SERVER_URL and ACTUAL_BUDGET_PASSWORD."
        ),
    )
    export.add_argument(
        "--output-dir",
        default=None,
        help=("Output directory for the CSV summary (default: /tmp)"),
    )
    export.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account IDs to export (default: all active)",
    )
    export.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="Days of transaction history to export (default: 90)",
    )
    export.add_argument(
        "--max-transactions",
        type=int,
        default=None,
        help="Hard limit on transactions to export per run",
    )

    # ── push ─────────────────────────────────────────────────────────
    push = ab_sub.add_parser(
        "push",
        help="Push transactions to a running Actual Budget instance",
        description=(
            "Export pending transactions from finance-sync and push them "
            "directly to a running Actual Budget self-hosted instance. "
            "Requires ACTUAL_BUDGET_SERVER_URL and ACTUAL_BUDGET_PASSWORD "
            "to be configured."
        ),
    )
    push.add_argument(
        "--server-url",
        default=None,
        help=(
            "Actual Budget server URL (overrides ACTUAL_BUDGET_SERVER_URL env)"
        ),
    )
    push.add_argument(
        "--password",
        default=None,
        help="Actual Budget password (overrides ACTUAL_BUDGET_PASSWORD env)",
    )
    push.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account IDs to push (default: all active)",
    )
    push.add_argument(
        "--days-back",
        type=int,
        default=90,
        help="Days of transaction history to push (default: 90)",
    )
    push.add_argument(
        "--max-transactions",
        type=int,
        default=None,
        help="Hard limit on transactions to push per run",
    )
    push.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be pushed without actually pushing",
    )

    return ab


def _build_securo_subparser(sub: Any) -> ArgumentParser:
    """Build the Securo CSV/API exporter parser."""
    securo = sub.add_parser(
        "securo", help="Export or push finance-sync transactions to Securo"
    )
    securo_sub = securo.add_subparsers(dest="securo_command", required=True)
    for command, help_text in (
        ("export", "Write Securo-compatible CSV files"),
        ("push", "Preview and import CSV files through Securo's API"),
    ):
        cmd = securo_sub.add_parser(command, help=help_text)
        cmd.add_argument("--server-url", default=None)
        cmd.add_argument("--email", default=None)
        cmd.add_argument("--password", default=None)
        cmd.add_argument("--output-dir", default=None)
        cmd.add_argument("--account-ids", default=None)
        cmd.add_argument("--days-back", type=int, default=90)
    return securo


def _build_ghostfolio_subparser(sub: Any) -> ArgumentParser:
    """Build the ``ghostfolio`` push parser."""
    gf = sub.add_parser(
        "ghostfolio",
        help="Push finance-sync transactions to Ghostfolio",
        description=(
            "Import booked finance-sync transactions into a self-hosted "
            "Ghostfolio through its JSON API. Requires "
            "GHOSTFOLIO_ACCESS_TOKEN."
        ),
    )
    push = gf.add_subparsers(dest="gf_command", required=True).add_parser(
        "push", help="Push transactions to Ghostfolio"
    )
    push.add_argument("--server-url", default=None)
    push.add_argument("--access-token", default=None)
    push.add_argument("--account-ids", default=None)
    push.add_argument("--days-back", type=int, default=90)
    push.add_argument("--max-transactions", type=int, default=None)
    push.add_argument("--dry-run", action="store_true", default=False)
    return gf


def _build_investbrain_subparser(sub: Any) -> ArgumentParser:
    """Build the ``investbrain`` push parser."""
    ib = sub.add_parser(
        "investbrain",
        help="Push finance-sync investment transactions to InvestBrain",
        description=(
            "Upsert investment accounts as InvestBrain portfolios and push "
            "booked purchase/sale transactions through its Sanctum API."
        ),
    )
    push = ib.add_subparsers(dest="ib_command", required=True).add_parser(
        "push", help="Push transactions to InvestBrain"
    )
    push.add_argument("--server-url", default=None)
    push.add_argument("--access-token", default=None)
    push.add_argument("--account-ids", default=None)
    push.add_argument("--days-back", type=int, default=90)
    push.add_argument("--max-transactions", type=int, default=None)
    push.add_argument("--dry-run", action="store_true", default=False)
    return ib


def _run_async(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a coroutine synchronously with proper event loop handling."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


async def _cmd_reconcile(args: Namespace) -> None:
    """Execute the ``reconcile`` subcommand."""
    settings = Settings()
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    container = Container.from_settings(settings)

    async with container.dispose():
        # Resolve tenants — either explicit --tenant-id or all from DB
        tenant_ids: list[str] = []
        if args.tenant_id:
            tenant_ids = [args.tenant_id]
        else:
            async with container.session_factory() as session:
                uow = UnitOfWork(session)
                tenants = await uow.tenants.list(limit=100)
                tenant_ids = [t.id for t in tenants if t.id]

            if not tenant_ids:
                print(
                    "ERROR: No tenants found in the database. "
                    "Provide --tenant-id or seed a tenant first.",
                    file=sys.stderr,
                )
                sys.exit(2)

        # Parse optional account IDs
        account_ids: list[str] | None = None
        if args.account_ids:
            account_ids = [
                a.strip() for a in args.account_ids.split(",") if a.strip()
            ]

        # Parse optional provider keys
        provider_keys: list[str] | None = None
        if args.provider_keys:
            provider_keys = [
                p.strip() for p in args.provider_keys.split(",") if p.strip()
            ]

        # If --connector-a/--connector-b given, use them as provider_keys
        if args.connector_a or args.connector_b:
            if not args.connector_a or not args.connector_b:
                print(
                    "ERROR: Both --connector-a and --connector-b must be "
                    "provided together.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if args.connector_a == args.connector_b:
                print(
                    "ERROR: --connector-a and --connector-b must be "
                    f"different, got '{args.connector_a}' for both.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if provider_keys:
                print(
                    "ERROR: --connector-a/--connector-b cannot be combined "
                    "with --provider-keys.",
                    file=sys.stderr,
                )
                sys.exit(2)
            provider_keys = [args.connector_a, args.connector_b]

        # Build date range — explicit --date-from/--date-to take priority
        date_to = datetime.now(UTC)
        if args.date_to:
            date_to = datetime.fromisoformat(args.date_to)

        if args.date_from:
            date_from = datetime.fromisoformat(args.date_from)
        else:
            date_from = date_to - timedelta(days=args.days_back)

        tenant_display = ", ".join(
            t[:8] + "…" if len(t) > 8 else t for t in tenant_ids
        )
        print(
            f"Reconciliation starting …\n"
            f"  Tenant(s):    {tenant_display}\n"
            f"  Date range:   {date_from.date()} → {date_to.date()}\n"
            f"  Account IDs:  {account_ids or 'all'}\n"
            f"  Providers:    {provider_keys or 'all'}\n"
            f"  Threshold:    {args.threshold_hours}h\n"
            f"  Duplicates:   {'yes' if args.detect_duplicates else 'no'}\n"
        )

        overall_findings = 0
        overall_failures = 0

        for tid in tenant_ids:
            svc = ReconciliationService(
                session_factory=container.session_factory,
                tenant_id=tid,
            )

            print(f"\n── Tenant {tid[:16]} ──")

            try:
                run = await svc.reconcile(
                    account_ids=account_ids,
                    provider_keys=provider_keys,
                    date_from=date_from,
                    date_to=date_to,
                    threshold_hours=args.threshold_hours,
                    detect_duplicates=args.detect_duplicates,
                )

                status = (
                    run.status.value
                    if hasattr(run.status, "value")
                    else str(run.status)
                )
                finding_count = run.finding_count or 0
                overall_findings += finding_count

                print(f"  Run ID:       {run.id}")
                print(f"  Run status:   {status}")
                print(f"  Findings:     {finding_count}")

                summary = run.summary or {}
                by_kind = summary.get("by_kind", {})
                by_severity = summary.get("by_severity", {})
                if by_kind:
                    print("  By kind:")
                    for kind, count in sorted(by_kind.items()):
                        print(f"    {kind}: {count}")
                if by_severity:
                    print("  By severity:")
                    for sev, count in sorted(by_severity.items()):
                        print(f"    {sev}: {count}")

                if run.status == ReconciliationRunStatus.FAILED:
                    overall_failures += 1
                    print(f"  ERROR: {run.error_message or 'Unknown error'}")

            except Exception as exc:
                overall_failures += 1
                print(f"  FAILED: {exc}")

        # Exit code
        print()
        if overall_failures > 0:
            print(f"✗ {overall_failures} tenant(s) failed.")
            sys.exit(2)
        elif overall_findings > 0:
            print(
                f"⚠  {overall_findings} finding(s) across"
                f" {len(tenant_ids)} tenant(s)"
                " — review recommended."
            )
            sys.exit(1)
        else:
            print(
                f"✓ No findings — all"
                f" {len(tenant_ids)} tenant(s) look consistent."
            )
            sys.exit(0)


async def _cmd_compare(args: Namespace) -> None:
    """Execute the ``compare`` subcommand.

    Runs reconciliation limited to two specified providers and prints
    the discrepancy report.
    """
    settings = Settings()
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    container = Container.from_settings(settings)

    async with container.dispose():
        # Resolve tenant
        tenant_id = args.tenant_id
        if not tenant_id:
            async with container.session_factory() as session:
                uow = UnitOfWork(session)
                tenants = await uow.tenants.list(limit=1)
                if not tenants:
                    print(
                        "ERROR: No tenants found in the database. "
                        "Provide --tenant-id or seed a tenant first.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                tenant_id = tenants[0].id

        # Build date range
        date_to = datetime.now(UTC)
        if args.date_to:
            date_to = datetime.fromisoformat(args.date_to)

        if args.date_from:
            date_from = datetime.fromisoformat(args.date_from)
        else:
            date_from = date_to - timedelta(days=90)

        # Validate connectors differ
        if args.connector_a == args.connector_b:
            print(
                "ERROR: Connector IDs must be different, got "
                f"'{args.connector_a}' for both.",
                file=sys.stderr,
            )
            sys.exit(2)

        print(
            f"Comparing connectors …\n"
            f"  Connector A:  {args.connector_a}\n"
            f"  Connector B:  {args.connector_b}\n"
            f"  Tenant:       {str(tenant_id)[:16]}…\n"
            f"  Date range:   {date_from.date()} → {date_to.date()}\n"
            f"  Threshold:    {args.threshold_hours}h\n"
        )

        svc = ReconciliationService(
            session_factory=container.session_factory,
            tenant_id=tenant_id,
        )

        try:
            run = await svc.reconcile(
                provider_keys=[args.connector_a, args.connector_b],
                date_from=date_from,
                date_to=date_to,
                threshold_hours=args.threshold_hours,
                detect_duplicates=args.detect_duplicates,
            )

            status = (
                run.status.value
                if hasattr(run.status, "value")
                else str(run.status)
            )
            finding_count = run.finding_count or 0

            print(f"  Run ID:       {run.id}")
            print(f"  Run status:   {status}")
            print(f"  Findings:     {finding_count}")

            if run.status == ReconciliationRunStatus.FAILED:
                print(
                    f"  FAILED: {run.error_message or 'Unknown error'}",
                    file=sys.stderr,
                )
                sys.exit(2)

            summary = run.summary or {}
            by_kind = summary.get("by_kind", {})
            by_severity = summary.get("by_severity", {})
            if by_kind:
                print("  By kind:")
                for kind, count in sorted(by_kind.items()):
                    print(f"    {kind}: {count}")
            if by_severity:
                print("  By severity:")
                for sev, count in sorted(by_severity.items()):
                    print(f"    {sev}: {count}")

            print(f"\nCompared '{args.connector_a}' vs '{args.connector_b}'")

            if finding_count > 0:
                print(f"⚠  {finding_count} finding(s) — review recommended.")
                sys.exit(1)
            else:
                print("✓ No findings — connectors look consistent.")
                sys.exit(0)

        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            sys.exit(2)


# ═══════════════════════════════════════════════════════════════════════
# Ghostfolio commands
# ═══════════════════════════════════════════════════════════════════════


async def _cmd_ghostfolio(args: Namespace) -> None:
    """Push canonical booked transactions to Ghostfolio."""
    settings = Settings()
    if not settings.exporter_ghostfolio_enabled:
        print("ERROR: Ghostfolio exporter is disabled.", file=sys.stderr)
        sys.exit(2)
    container = Container.from_settings(settings)
    async with container.dispose():
        async with container.session_factory() as session:
            tenants = await UnitOfWork(session).tenants.list(limit=1)
            if not tenants:
                print(
                    "ERROR: No tenants found in the database.", file=sys.stderr
                )
                sys.exit(2)
            tenant_id = tenants[0].id
        from finance_sync.exporter.ghostfolio.client import GhostfolioClient
        from finance_sync.exporter.ghostfolio.config import GhostfolioConfig
        from finance_sync.exporter.ghostfolio.exporter import GhostfolioExporter

        token = args.access_token or secret_value(
            settings.ghostfolio_access_token
        )
        if not token:
            print(
                "ERROR: Set GHOSTFOLIO_ACCESS_TOKEN or pass --access-token.",
                file=sys.stderr,
            )
            sys.exit(2)

        config = GhostfolioConfig(
            server_url=args.server_url or settings.ghostfolio_server_url,
            access_token=token,
            request_timeout=settings.ghostfolio_request_timeout,
            verify_ssl=settings.ghostfolio_verify_ssl,
            data_source=settings.ghostfolio_data_source,
            include_pending=settings.ghostfolio_include_pending,
            sync_transactions=settings.ghostfolio_sync_transactions,
        )
        account_ids = (
            [x.strip() for x in args.account_ids.split(",") if x.strip()]
            if args.account_ids
            else None
        )
        since = datetime.now(UTC) - timedelta(days=args.days_back)
        exporter = GhostfolioExporter(
            container.session_factory, config, tenant_id
        )
        async with GhostfolioClient(config) as client:
            await client.health()
            if args.dry_run:
                print("Ghostfolio is healthy; dry-run does not import data.")
                return
            result = await exporter.run_export(
                client,
                since=since,
                account_ids=account_ids,
                max_transactions=args.max_transactions,
            )
        print(result)
        if result["status"] != "completed":
            sys.exit(2)


async def _cmd_investbrain(args: Namespace) -> None:
    """Push canonical investment transactions to InvestBrain."""
    settings = Settings()
    if not settings.exporter_investbrain_enabled:
        print("ERROR: InvestBrain exporter is disabled.", file=sys.stderr)
        sys.exit(2)
    container = Container.from_settings(settings)
    async with container.dispose():
        async with container.session_factory() as session:
            tenants = await UnitOfWork(session).tenants.list(limit=1)
            if not tenants:
                print(
                    "ERROR: No tenants found in the database.", file=sys.stderr
                )
                sys.exit(2)
            tenant_id = tenants[0].id
        from finance_sync.exporter.investbrain.client import InvestBrainClient
        from finance_sync.exporter.investbrain.config import InvestBrainConfig
        from finance_sync.exporter.investbrain.exporter import (
            InvestBrainExporter,
        )

        token = args.access_token or secret_value(
            settings.investbrain_access_token
        )
        if not token:
            print(
                "ERROR: Set INVESTBRAIN_ACCESS_TOKEN or pass --access-token.",
                file=sys.stderr,
            )
            sys.exit(2)
        config = InvestBrainConfig(
            server_url=args.server_url or settings.investbrain_server_url,
            access_token=token,
            request_timeout=settings.investbrain_request_timeout,
            verify_ssl=settings.investbrain_verify_ssl,
            include_pending=settings.investbrain_include_pending,
            portfolio_name_prefix=settings.investbrain_portfolio_name_prefix,
        )
        account_ids = (
            [x.strip() for x in args.account_ids.split(",") if x.strip()]
            if args.account_ids
            else None
        )
        exporter = InvestBrainExporter(
            container.session_factory, config, tenant_id
        )
        async with InvestBrainClient(config) as client:
            await client.health()
            if args.dry_run:
                print("InvestBrain is healthy; dry-run does not import data.")
                return
            result = await exporter.run_export(
                client,
                since=datetime.now(UTC) - timedelta(days=args.days_back),
                account_ids=account_ids,
                max_transactions=args.max_transactions,
            )
        print(result)
        if result["status"] != "completed":
            sys.exit(2)


# ═══════════════════════════════════════════════════════════════════════
# Wealthfolio commands
# ═══════════════════════════════════════════════════════════════════════


async def _cmd_wealthfolio(args: Namespace) -> None:
    """Execute the ``wealthfolio`` subcommand."""
    settings = Settings()
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    if not settings.exporter_wealthfolio_enabled:
        print(
            "ERROR: Wealthfolio exporter is disabled "
            "(EXPORTER_WEALTHFOLIO_ENABLED=false).",
            file=sys.stderr,
        )
        sys.exit(2)

    container = Container.from_settings(settings)

    async with container.dispose():
        tenant_id = getattr(args, "tenant_id", None)
        if not tenant_id:
            async with container.session_factory() as session:
                uow = UnitOfWork(session)
                tenants = await uow.tenants.list(limit=1)
                if not tenants:
                    print(
                        "ERROR: No tenants found in the database.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                tenant_id = tenants[0].id

        if args.wf_command == "export":
            await _cmd_wealthfolio_export(args, container, tenant_id)
        elif args.wf_command == "push":
            await _cmd_wealthfolio_push(args, container, tenant_id)
        elif args.wf_command == "smoke":
            await _cmd_wealthfolio_smoke(args, container, tenant_id)
        else:
            print(f"Unknown wealthfolio command: {args.wf_command}")
            sys.exit(2)


async def _cmd_wealthfolio_export(
    args: Namespace,
    container: Container,
    tenant_id: str,
) -> None:
    """Export transactions to Wealthfolio CSV files."""
    from pathlib import Path

    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    wf_config = WealthfolioConfig.from_settings(container.settings)
    output_dir = args.output_dir or str(wf_config.output_dir)

    print("Wealthfolio CSV export starting …")
    print(f"  Output dir:   {output_dir}")
    print(f"  Tenant:       {str(tenant_id)[:16]}…")
    print(f"  Days back:    {args.days_back}")

    since = (
        None
        if args.full_history or args.rebuild
        else datetime.now(UTC) - timedelta(days=args.days_back)
    )

    exporter = WealthfolioExporter(
        session_factory=container.session_factory,
        wf_config=wf_config,
        tenant_id=tenant_id,
    )

    result = await exporter.run_export(
        since=since,
        output_dir=Path(output_dir),
    )

    print(f"\nResult: {result.status}")
    if result.status == "completed":
        print(
            f"  Transactions: {result.transactions_exported}"
            f"/{result.transactions_attempted}"
        )
        print(f"  Holdings:     {result.holdings_exported}")
        print(f"  CSV files:    {len(result.csv_files)}")
        for f in result.csv_files:
            print(f"    {f}")
    else:
        print(f"  ERROR: {result.error_message}")
        sys.exit(2)


async def _cmd_wealthfolio_push(
    args: Namespace,
    container: Container,
    tenant_id: str,
) -> None:
    """Push transactions directly to a running Wealthfolio instance."""
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    # Resolve server URL and password
    server_url = args.server_url or getattr(
        container.settings, "wealthfolio_server_url", ""
    )
    password = args.password or secret_value(
        container.settings.wealthfolio_password
    )

    if not server_url:
        print(
            "ERROR: Wealthfolio server URL not configured.\n"
            "  Set WEALTHFOLIO_SERVER_URL env var or pass --server-url.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not password:
        print(
            "ERROR: Wealthfolio password not configured.\n"
            "  Set WEALTHFOLIO_PASSWORD env var or pass --password.",
            file=sys.stderr,
        )
        sys.exit(2)

    wf_config = WealthfolioConfig.from_settings(container.settings)
    since = datetime.now(UTC) - timedelta(days=args.days_back)

    print("Wealthfolio push starting …")
    print(f"  Server URL:   {server_url}")
    print(f"  Tenant:       {str(tenant_id)[:16]}…")
    print(
        "  History:      full/rebuild"
        if args.full_history or args.rebuild
        else f"  Days back:    {args.days_back}"
    )

    exporter = WealthfolioExporter(
        session_factory=container.session_factory,
        wf_config=wf_config,
        tenant_id=tenant_id,
    )

    account_ids: list[str] | None = None
    if args.account_ids:
        account_ids = [
            a.strip() for a in args.account_ids.split(",") if a.strip()
        ]

    if args.dry_run:
        # Count transactions without pushing
        accounts = await exporter._load_accounts(account_ids)  # noqa: SLF001
        total = 0
        for acct in accounts:
            txns = await exporter._fetch_pending_transactions(  # noqa: SLF001
                account_id=acct.id,
                since=since,
            )
            total += len(txns)
            print(f"  [{acct.name}] {len(txns)} pending transactions")

        print(f"\nDry run: {total} transaction(s) ready to push.")
        print("Use --no-dry-run or omit --dry-run to push.")
        return

    # Authenticate and push
    wf_client_config = WealthfolioClientConfig(
        base_url=server_url,
        password=password,
        request_timeout=container.settings.wealthfolio_request_timeout,
    )
    wf_client = WealthfolioClient(config=wf_client_config)

    try:
        print("  Authenticating …")
        await wf_client.authenticate()
        print("  ✓ Authenticated")

        result = await exporter.push_to_wealthfolio(
            wf_client=wf_client,
            accounts=await exporter._load_accounts(account_ids),  # noqa: SLF001
            since=since,
            full_sync=args.full_history or args.rebuild,
            rebuild=args.rebuild,
        )
        print("\nResult:")
        print(f"  Imported:     {result.get('imported', 0)}")
        print(f"  Skipped:     {result.get('skipped', 0)}")
        print(f"  Failed:      {result.get('failed', 0)}")

    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        await wf_client.close()


async def _cmd_wealthfolio_smoke(
    args: Namespace,
    container: Container,
    tenant_id: str,
) -> None:
    """Verify account visibility, reconciliation and cursor idempotency."""
    from finance_sync.exporter.wealthfolio.client import (
        WealthfolioClient,
        WealthfolioClientConfig,
    )
    from finance_sync.exporter.wealthfolio.config import WealthfolioConfig
    from finance_sync.exporter.wealthfolio.exporter import WealthfolioExporter

    server_url = args.server_url or container.settings.wealthfolio_server_url
    password = args.password or secret_value(
        container.settings.wealthfolio_password
    )
    if not server_url or not password:
        print(
            "ERROR: configure WEALTHFOLIO_SERVER_URL and WEALTHFOLIO_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Issue #504 guard: a smoke push created the corrupted 'Smoke Test
    # Brokerage' account in production (NULL-asset BUY row -> slow holdings
    # recalculation -> HTTP 408).  Refuse to write to the production
    # instance unless the operator explicitly opts in.
    if (
        not getattr(args, "allow_prod", False)
        and server_url.rstrip("/") in _WF_PROD_BASE_URLS
    ):
        print(
            "Refusing to run the Wealthfolio smoke push against the "
            "production instance without --allow-prod (issue #504 guard).",
            file=sys.stderr,
        )
        sys.exit(2)
    account_ids = (
        [
            value.strip()
            for value in args.account_ids.split(",")
            if value.strip()
        ]
        if args.account_ids
        else None
    )
    exporter = WealthfolioExporter(
        session_factory=container.session_factory,
        wf_config=WealthfolioConfig.from_settings(container.settings),
        tenant_id=tenant_id,
    )
    accounts = await exporter._load_accounts(account_ids)  # noqa: SLF001
    client = WealthfolioClient(
        WealthfolioClientConfig(
            base_url=server_url,
            password=password,
            request_timeout=container.settings.wealthfolio_request_timeout,
        )
    )
    try:
        await client.authenticate()
        since = datetime.now(UTC) - timedelta(days=args.days_back)
        first = await exporter.push_to_wealthfolio(
            client, accounts=accounts, since=since, full_sync=True
        )
        second = await exporter.push_to_wealthfolio(
            client, accounts=accounts, since=since
        )
        visible = 0
        activity_count = 0
        holding_count = 0
        for account in accounts:
            remote = await exporter._ensure_wf_account(  # noqa: SLF001
                client, account
            )
            remote_id = str(remote["id"])
            activities = await client.search_activities(remote_id)
            meta = activities.get("meta")
            typed_meta = (
                cast("dict[str, Any]", meta) if isinstance(meta, dict) else {}
            )
            total_rows = typed_meta.get("totalRowCount", 0)
            activity_count += total_rows if isinstance(total_rows, int) else 0
            holding_count += len(await client.get_holdings(remote_id))
            visible += 1
        healthy = (
            bool(accounts)
            and visible == len(accounts)
            and not first.get("errors")
            and not second.get("errors")
            and second.get("imported", 0) == 0
        )
        print("Wealthfolio smoke result:")
        print(f"  Accounts visible: {visible}")
        print(f"  Activities visible: {activity_count}")
        print(f"  Holdings visible: {holding_count}")
        print(f"  Idempotent second pass: {'yes' if healthy else 'no'}")
        if not healthy:
            sys.exit(1)
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════════════
# Actual Budget commands
# ═══════════════════════════════════════════════════════════════════════


async def _cmd_actual_budget(args: Namespace) -> None:
    """Execute the ``actual-budget`` subcommand."""
    settings = Settings()
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

    if not settings.exporter_actual_budget_enabled:
        print(
            "ERROR: Actual Budget exporter is disabled "
            "(EXPORTER_ACTUAL_BUDGET_ENABLED=false).",
            file=sys.stderr,
        )
        sys.exit(2)

    container = Container.from_settings(settings)

    async with container.dispose():
        tenant_id = getattr(args, "tenant_id", None)
        if not tenant_id:
            async with container.session_factory() as session:
                uow = UnitOfWork(session)
                tenants = await uow.tenants.list(limit=1)
                if not tenants:
                    print(
                        "ERROR: No tenants found in the database.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                tenant_id = tenants[0].id

        if args.ab_command == "export":
            await _cmd_actual_budget_export(args, container, tenant_id)
        elif args.ab_command == "push":
            await _cmd_actual_budget_push(args, container, tenant_id)
        else:
            print(f"Unknown actual-budget command: {args.ab_command}")
            sys.exit(2)


async def _cmd_securo(args: Namespace) -> None:
    """Run the Securo CSV/API export cycle."""
    settings = Settings()
    if not settings.exporter_securo_enabled:
        print("ERROR: Securo exporter is disabled.", file=sys.stderr)
        sys.exit(2)
    from finance_sync.exporter.securo.config import SecuroConfig
    from finance_sync.exporter.securo.exporter import SecuroExporter

    container = Container.from_settings(settings)
    async with container.dispose():
        async with container.session_factory() as session:
            tenants = await UnitOfWork(session).tenants.list(limit=1)
            if not tenants:
                print(
                    "ERROR: No tenants found in the database.", file=sys.stderr
                )
                sys.exit(2)
            tenant_id = tenants[0].id
        account_ids = (
            [v.strip() for v in args.account_ids.split(",") if v.strip()]
            if args.account_ids
            else None
        )
        config = SecuroConfig(
            server_url=args.server_url or settings.securo_server_url,
            email=args.email or settings.securo_email,
            password=args.password or secret_value(settings.securo_password),
            output_dir=args.output_dir or settings.securo_output_dir,
            auto_create_accounts=settings.securo_auto_create_accounts,
        )
        result = await SecuroExporter(
            container.session_factory, config, tenant_id
        ).run_export(
            since=datetime.now(UTC) - timedelta(days=args.days_back),
            account_ids=account_ids,
            output_dir=args.output_dir,
            push=args.securo_command == "push",
        )
        print(f"Status: {result.status}")
        print(
            "Transactions: "
            f"{result.transactions_imported}/{result.transactions_attempted} "
            f"(skipped: {result.transactions_skipped})"
        )
        print(
            "Holdings: "
            f"{result.holdings_imported}/{result.holdings_attempted} "
            f"(updated: {result.holdings_skipped})"
        )
        for path in result.files:
            print(f"CSV: {path}")
        if result.error_message:
            print(f"ERROR: {result.error_message}", file=sys.stderr)
            sys.exit(2)


async def _cmd_actual_budget_export(
    args: Namespace,
    container: Container,
    tenant_id: str,
) -> None:
    """Run the Actual Budget export cycle (CSV + server import)."""
    import os
    from pathlib import Path

    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )

    ab_config = ActualBudgetConfig.from_settings(container.settings)
    output_dir = args.output_dir or str(
        Path("/tmp") / "finance_sync_ab_exports"
    )
    os.makedirs(output_dir, exist_ok=True)

    since = datetime.now(UTC) - timedelta(days=args.days_back)

    account_ids: list[str] | None = None
    if args.account_ids:
        account_ids = [
            a.strip() for a in args.account_ids.split(",") if a.strip()
        ]

    print("Actual Budget export starting …")
    print(f"  Server URL:   {ab_config.server_url}")
    print(f"  Output dir:   {output_dir}")
    print(f"  Tenant:       {str(tenant_id)[:16]}…")
    print(f"  Days back:    {args.days_back}")

    exporter = ActualBudgetExporter(
        session_factory=container.session_factory,
        ab_config=ab_config,
        tenant_id=tenant_id,
    )

    result = await exporter.run_export(
        since=since,
        account_ids=account_ids,
        max_transactions=args.max_transactions,
        output_dir=output_dir,
    )

    print(f"\nResult: {result.status}")
    if result.status == "completed":
        print(
            f"  Transactions: {result.transactions_exported}"
            f"/{result.transactions_attempted}"
        )
        print(f"  Accounts:     {result.accounts_mapped}")
        print(f"  CSV:          {output_dir}/ab_export_*.csv")
        sys.exit(0)
    else:
        print(f"  ERROR: {result.error_message}", file=sys.stderr)
        sys.exit(2)


async def _cmd_actual_budget_push(
    args: Namespace,
    container: Container,
    tenant_id: str,
) -> None:
    """Push transactions directly to a running Actual Budget instance."""
    from finance_sync.exporter.actual_budget.config import ActualBudgetConfig
    from finance_sync.exporter.actual_budget.exporter import (
        ActualBudgetExporter,
    )

    # Resolve server URL and password
    server_url = args.server_url or getattr(
        container.settings, "actual_budget_server_url", ""
    )
    password = args.password or secret_value(
        container.settings.actual_budget_password
    )

    if not server_url:
        print(
            "ERROR: Actual Budget server URL not configured.\n"
            "  Set ACTUAL_BUDGET_SERVER_URL env var or pass --server-url.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not password:
        print(
            "ERROR: Actual Budget password not configured.\n"
            "  Set ACTUAL_BUDGET_PASSWORD env var or pass --password.",
            file=sys.stderr,
        )
        sys.exit(2)

    ab_config = ActualBudgetConfig.from_settings(container.settings).model_copy(
        update={"server_url": server_url, "password": password}
    )
    since = datetime.now(UTC) - timedelta(days=args.days_back)

    print("Actual Budget push starting …")
    print(f"  Server URL:   {server_url}")
    print(f"  Tenant:       {str(tenant_id)[:16]}…")
    print(f"  Days back:    {args.days_back}")

    exporter = ActualBudgetExporter(
        session_factory=container.session_factory,
        ab_config=ab_config,
        tenant_id=tenant_id,
    )

    account_ids: list[str] | None = None
    if args.account_ids:
        account_ids = [
            a.strip() for a in args.account_ids.split(",") if a.strip()
        ]

    if args.dry_run:
        # Count transactions without pushing
        accounts = await exporter._load_accounts(account_ids)  # noqa: SLF001
        total = 0
        for acct in accounts:
            async with container.session_factory() as session:
                txns = await exporter._fetch_pending_transactions(  # noqa: SLF001
                    session,
                    account_id=acct.id,
                    since=since,
                )
            total += len(txns)
            print(f"  [{acct.name}] {len(txns)} pending transactions")

        print(f"\nDry run: {total} transaction(s) ready to push.")
        print("Use --no-dry-run or omit --dry-run to push.")
        sys.exit(0)

    try:
        result = await exporter.run_export(
            since=since,
            account_ids=account_ids,
            max_transactions=args.max_transactions,
        )
        print("\nResult:")
        print(f"  Status:       {result.status}")
        print(
            f"  Transactions: {result.transactions_exported}"
            f"/{result.transactions_attempted}"
        )
        print(f"  Failed:       {result.transactions_failed}")
        if result.status != "completed":
            print(f"  ERROR: {result.error_message}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Nothing.  Exits with one of:
        - ``0``  Success, no discrepancies.
        - ``1``  Success, discrepancies found.
        - ``2``  Internal error (settings, DB, unexpected exception).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "reconcile":
        _run_async(_cmd_reconcile(args))
    elif args.command == "compare":
        _run_async(_cmd_compare(args))
    elif args.command == "wealthfolio":
        _run_async(_cmd_wealthfolio(args))
    elif args.command == "actual-budget":
        _run_async(_cmd_actual_budget(args))
    elif args.command == "securo":
        _run_async(_cmd_securo(args))
    elif args.command == "ghostfolio":
        _run_async(_cmd_ghostfolio(args))
    elif args.command == "investbrain":
        _run_async(_cmd_investbrain(args))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
