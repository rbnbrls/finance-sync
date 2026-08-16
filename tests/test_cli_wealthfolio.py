"""CLI contract tests for Wealthfolio commands."""

from finance_sync.cli import _build_parser


def test_wealthfolio_smoke_parser_accepts_account_filter() -> None:
    """The production smoke check can target one or more local accounts."""
    args = _build_parser().parse_args(
        ["wealthfolio", "smoke", "--account-ids", "account-1,account-2"]
    )

    assert args.command == "wealthfolio"
    assert args.wf_command == "smoke"
    assert args.account_ids == "account-1,account-2"
