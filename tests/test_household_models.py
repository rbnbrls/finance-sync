"""Unit tests for the household-sharing domain model (t_96e210df).

Covers the tenant-scoped invitation model (single-use, expiring,
sanitised representation) and the account ownership / visibility
fields (explicit policy, private-by-default).
"""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finance_sync.models.account import Account
from finance_sync.models.credential import Credential
from finance_sync.models.enums import (
    AccountVisibility,
    InvitationStatus,
    UserRole,
)
from finance_sync.models.household_invitation import (
    INVITATION_TTL_DAYS,
    HouseholdInvitation,
)

# ── HouseholdInvitation ────────────────────────────────────────────────


class TestHouseholdInvitation:
    def _invitation(
        self,
        *,
        status: str = InvitationStatus.PENDING,
        role: str = UserRole.USER,
        expires_at: datetime | None = None,
    ) -> HouseholdInvitation:
        return HouseholdInvitation(
            tenant_id="tenant-1",
            email="guest@example.com",
            token_hash="a" * 64,
            role=role,
            status=status,
            expires_at=expires_at or datetime.now(UTC) + timedelta(days=1),
            created_by="admin-1",
        )

    def test_default_expiry_is_ttl_days_ahead(self) -> None:
        """Invitations expire INVITATION_TTL_DAYS days after creation."""
        expiry = HouseholdInvitation.default_expiry()
        delta = expiry - datetime.now(UTC)
        assert timedelta(days=INVITATION_TTL_DAYS - 1) < delta
        assert delta <= timedelta(days=INVITATION_TTL_DAYS)

    def test_is_pending_true_for_future_expiry(self) -> None:
        """A pending invitation with a future expiry is accept-able."""
        invite = self._invitation()
        assert invite.is_pending is True

    def test_is_pending_false_when_expired(self) -> None:
        """A pending invitation past its expiry can no longer be used."""
        invite = self._invitation(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert invite.is_pending is False

    def test_is_pending_false_when_not_pending(self) -> None:
        """Accepted/revoked invitations are never pending."""
        for status in (
            InvitationStatus.ACCEPTED,
            InvitationStatus.REVOKED,
            InvitationStatus.EXPIRED,
        ):
            invite = self._invitation(status=status)
            assert invite.is_pending is False

    def test_to_dict_never_exposes_token_hash(self) -> None:
        """The public representation must not leak the token digest."""
        invite = self._invitation()
        payload = invite.to_dict()
        assert "token_hash" not in payload
        assert "accepted_by" not in payload
        assert payload["email"] == "guest@example.com"
        assert payload["role"] == UserRole.USER
        assert payload["status"] == InvitationStatus.PENDING

    def test_column_defaults_are_pending_and_user_role(self) -> None:
        """Fresh rows default to pending status with the user role."""
        status_col = HouseholdInvitation.__table__.columns["status"]
        role_col = HouseholdInvitation.__table__.columns["role"]
        assert status_col.default.arg == InvitationStatus.PENDING
        assert role_col.default.arg == UserRole.USER


# ── Account ownership & visibility ─────────────────────────────────────


class TestAccountVisibilityFields:
    def test_visibility_column_is_private_by_default(self) -> None:
        """Private-by-default is enforced at both ORM and DB level."""
        col = Account.__table__.columns["visibility"]
        assert col.default.arg == AccountVisibility.PRIVATE
        assert col.server_default.arg == AccountVisibility.PRIVATE
        assert col.nullable is False

    def test_owner_user_id_is_nullable(self) -> None:
        """Unowned (legacy/system) accounts are representable."""
        col = Account.__table__.columns["owner_user_id"]
        assert col.nullable is True

    def test_explicit_household_and_owner(self) -> None:
        """Sharing is an explicit opt-in, never inferred."""
        account = Account(
            tenant_id="tenant-1",
            provider_key="bunq",
            external_account_id="ext-1",
            name="Shared",
            account_type="checking",
            visibility=AccountVisibility.HOUSEHOLD,
            owner_user_id="user-1",
        )
        assert account.visibility == AccountVisibility.HOUSEHOLD
        assert account.owner_user_id == "user-1"

    def test_credential_owner_is_optional(self) -> None:
        """Legacy/system credentials may lack an owner (provenance NULL)."""
        credential = Credential(
            tenant_id="tenant-1",
            provider_key="bunq",
            encrypted_payload=b"\x00" * 32,
            nonce=b"\x00" * 12,
        )
        assert credential.owner_user_id is None
