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
from typing import Any

from finance_sync.config.settings import Settings
from finance_sync.container import Container
from finance_sync.db.uow import UnitOfWork
from finance_sync.models.enums import ReconciliationRunStatus
from finance_sync.observability.logging import configure_logging
from finance_sync.services.reconciliation import ReconciliationService


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


def _run_async(coro) -> None:
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
            f"  Tenant:       {tenant_id[:16]}…\n"
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
# Wealthfolio commands
# ═══════════════════════════════════════════════════════════════════════


async def _cmd_wealthfolio(args: Namespace) -> None:
    """Execute the ``wealthfolio`` subcommand."""
    settings = Settings()
    configure_logging(
        json_output=settings.is_production,
        log_level=settings.log_level,
    )

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
    print(f"  Tenant:       {tenant_id[:16]}…")
    print(f"  Days back:    {args.days_back}")

    since = datetime.now(UTC) - timedelta(days=args.days_back)

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
    password = args.password or getattr(
        container.settings, "wealthfolio_password", ""
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
    print(f"  Tenant:       {tenant_id[:16]}…")
    print(f"  Days back:    {args.days_back}")

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
        accounts = await exporter._load_accounts(account_ids)
        total = 0
        for acct in accounts:
            txns = await exporter._fetch_pending_transactions(
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
    )
    wf_client = WealthfolioClient(config=wf_client_config)

    try:
        print("  Authenticating …")
        await wf_client.authenticate()
        print("  ✓ Authenticated")

        result = await exporter.push_to_wealthfolio(
            wf_client=wf_client,
            since=since,
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
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
