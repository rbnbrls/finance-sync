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


def test_wealthfolio_smoke_parser_has_allow_prod_flag() -> None:
    """The smoke push requires an explicit --allow-prod for production."""
    args = _build_parser().parse_args(["wealthfolio", "smoke"])

    assert args.command == "wealthfolio"
    assert args.wf_command == "smoke"
    # Issue #504 guard: default is off, opt-in only.
    assert args.allow_prod is False


def test_wealthfolio_smoke_parser_allow_prod_opt_in() -> None:
    """--allow-prod can be passed explicitly."""
    args = _build_parser().parse_args(["wealthfolio", "smoke", "--allow-prod"])

    assert args.command == "wealthfolio"
    assert args.wf_command == "smoke"
    assert args.allow_prod is True
