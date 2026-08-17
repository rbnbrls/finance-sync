"""Unit tests for the per-connection account-selection helper.

Pure predicate tests: legacy rows (no connection scope) and orphaned
rows are always kept, connections without a selection export everything,
and selected connections are restricted to their pinned account ids.
"""

from __future__ import annotations

from types import SimpleNamespace

from finance_sync.services.account_selection import account_is_selected


def _account(external_id: str, connection_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        external_account_id=external_id, connection_id=connection_id
    )


class TestAccountIsSelected:
    def test_legacy_row_without_connection_scope_is_kept(self) -> None:
        """Rows imported before multi-connection must keep exporting."""
        assert account_is_selected(_account("acc_1", None), {})

    def test_orphaned_connection_row_is_kept(self) -> None:
        """A deleted connection must not silently hide its data."""
        assert account_is_selected(_account("acc_1", "gone"), {})

    def test_connection_without_selection_exports_all(self) -> None:
        selection = {"conn-1": None}
        assert account_is_selected(_account("acc_x", "conn-1"), selection)
        assert account_is_selected(_account("acc_y", "conn-1"), selection)

    def test_selected_account_is_exported(self) -> None:
        selection = {"conn-1": {"acc_1", "acc_2"}}
        assert account_is_selected(_account("acc_1", "conn-1"), selection)

    def test_deselected_account_is_not_exported(self) -> None:
        selection = {"conn-1": {"acc_1"}}
        assert not account_is_selected(_account("acc_2", "conn-1"), selection)

    def test_empty_selection_list_means_all(self) -> None:
        """selected_accounts=[] (legacy API shape) exports everything."""
        selection = {"conn-1": set()}
        assert account_is_selected(_account("acc_9", "conn-1"), selection)

    def test_same_external_id_in_two_connections_independent(self) -> None:
        selection = {"conn-a": {"acc_1"}, "conn-b": None}
        # Same provider account id: selected in A, unrestricted in B.
        assert account_is_selected(_account("acc_1", "conn-a"), selection)
        assert account_is_selected(_account("acc_1", "conn-b"), selection)
        # Deselected in A's sibling connection C.
        selection_c = {"conn-c": {"acc_2"}}
        assert not account_is_selected(_account("acc_1", "conn-c"), selection_c)
