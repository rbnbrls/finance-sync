"""Tax lot management service — cost basis, realised/unrealised P&L, wash sales.

Core responsibilities
--------------------
1. **Lot creation** — scan purchase transactions and create/open tax lots.
2. **Cost-basis matching** — match sell transactions against open lots using
   FIFO (default) or LIFO.
3. **Realised P&L** — compute realised P&L when a lot is fully or partially
   closed.
4. **Unrealised P&L** — compute unrealised P&L for open lots against current
   market prices.
5. **Wash sales** — detect wash sale patterns and adjust cost basis.

Design notes
------------
- The service is stateless; all state lives in the DB via the repositories.
- Methods accept an explicit ``cost_basis_method`` parameter (defaults to FIFO).
- Partial sales split an existing lot: the sold portion is recorded as a
  closed sub-lot and a new open lot carries the remaining shares forward.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from finance_sync.db.repositories import TaxLotRepository
from finance_sync.models.enums import CostBasisMethod, TransactionType
from finance_sync.models.tax_lot import TaxLot
from finance_sync.models.transaction import Transaction
from finance_sync.services.wealthfolio_preflight import quantity_event_ratio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

E = Decimal

# ── Constants ──────────────────────────────────────────────────────────

WASH_SALE_LOOKBACK_DAYS = 30
"""Number of days before/after a sale to check for wash-sale repurchases."""


def _transfer_reference(transaction: Transaction) -> str | None:
    """Read the allowlisted provider transfer reference from canonical data."""
    metadata = transaction.provider_metadata_contract
    if not isinstance(metadata, dict):
        return transaction.counterparty_account_reference or None
    candidates: list[dict[str, Any]] = [metadata]
    fields = metadata.get("fields")
    if isinstance(fields, dict):
        candidates.append(cast("dict[str, Any]", fields))
    for candidate in candidates:
        for key in (
            "transfer_id",
            "transferId",
            "transfer_reference",
            "transferReference",
        ):
            value = candidate.get(key)
            if value not in (None, ""):
                return str(value).strip() or None
    return transaction.counterparty_account_reference or None


def pair_security_transfer_legs(
    transactions: list[Transaction],
) -> dict[str, Transaction]:
    """Pair security-transfer legs without guessing unidentifiable matches.

    A pair requires the same provider/counterparty reference, security,
    currency, calendar date and absolute quantity, plus opposite signed cash
    amounts and different accounts.  The returned map is symmetric.
    """
    groups: dict[tuple[str, str, str, str, str], list[Transaction]] = {}
    for transaction in transactions:
        if str(transaction.transaction_type) != TransactionType.TRANSFER.value:
            continue
        if transaction.security_id is None or not transaction.occurred_at:
            continue
        reference = _transfer_reference(transaction)
        if not reference:
            continue
        try:
            quantity = abs(Decimal(str(transaction.quantity)))
            amount = Decimal(str(transaction.amount))
        except Exception:
            continue
        if quantity <= 0 or amount == 0:
            continue
        key = (
            str(transaction.security_id),
            str(transaction.currency_code).upper(),
            transaction.occurred_at.date().isoformat(),
            str(quantity),
            reference,
        )
        groups.setdefault(key, []).append(transaction)

    pairs: dict[str, Transaction] = {}
    for rows in groups.values():
        outbound = sorted(
            (row for row in rows if Decimal(str(row.amount)) < 0),
            key=lambda row: str(row.id),
        )
        inbound = sorted(
            (row for row in rows if Decimal(str(row.amount)) > 0),
            key=lambda row: str(row.id),
        )
        for source, destination in zip(outbound, inbound, strict=False):
            if str(source.account_id) == str(destination.account_id):
                continue
            pairs[str(source.id)] = destination
            pairs[str(destination.id)] = source
    return pairs


async def transfer_lots_to_destination(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
    destination: Transaction,
) -> dict[str, Any]:
    """Move FIFO lot basis across a paired security transfer."""
    quantity = abs(Decimal(str(transaction.quantity or 0)))
    if quantity <= 0 or Decimal(str(transaction.amount)) >= 0:
        return {"action": "transfer_basis_skipped", "reason": "not_outbound"}

    repo = TaxLotRepository(session)
    if await repo.find_lots_for_transfer(tenant_id, str(destination.id)):
        return {
            "action": "transfer_basis_already_applied",
            "transfer_id": str(destination.id),
        }
    open_lots = await repo.find_open_lots(
        tenant_id,
        str(transaction.account_id),
        str(transaction.security_id),
    )
    remaining = quantity
    moved = E("0")
    allocations: list[tuple[TaxLot, Decimal]] = []
    for lot in open_lots:
        if remaining <= 0:
            break
        available = max(E("0"), lot.remaining_quantity)
        moved_quantity = min(available, remaining)
        if moved_quantity <= 0:
            continue
        allocations.append((lot, moved_quantity))
        remaining -= moved_quantity
        moved += moved_quantity
    if remaining > 0:
        return {
            "action": "transfer_basis_gap",
            "transfer_id": str(destination.id),
            "quantity_requested": str(quantity),
            "quantity_moved": str(moved),
            "quantity_unmatched": str(remaining),
            "lots_created": len(allocations),
        }
    for lot, moved_quantity in allocations:
        lot.remaining_quantity = lot.remaining_quantity - moved_quantity
        if lot.remaining_quantity == 0:
            lot.closed_at = transaction.occurred_at
        await repo.update(lot)
        await repo.add(
            TaxLot(
                tenant_id=tenant_id,
                account_id=str(destination.account_id),
                security_id=str(destination.security_id),
                purchase_transaction_id=None,
                transfer_transaction_id=str(destination.id),
                quantity=moved_quantity,
                remaining_quantity=moved_quantity,
                cost_basis_total=moved_quantity * lot.cost_basis_per_unit,
                cost_basis_per_unit=lot.cost_basis_per_unit,
                currency_code=lot.currency_code,
                acquired_at=lot.acquired_at,
                cost_basis_method=lot.cost_basis_method,
            )
        )
    return {
        "action": "transfer_basis_moved",
        "transfer_id": str(destination.id),
        "quantity_moved": str(moved),
        "lots_created": len(allocations),
    }


# ── Public API ─────────────────────────────────────────────────────────


async def create_tax_lots_for_purchase(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
) -> TaxLot:
    """Create a new open tax lot from a purchase transaction.

    The lot records the full quantity and total cost from the
    transaction.  The cost per unit is computed as ``abs(amount) / qty``
    (amount is negative for purchases in this schema).
    """
    assert transaction.transaction_type == TransactionType.PURCHASE
    quantity = abs(transaction.quantity) if transaction.quantity else E("0")

    cost_basis_total = abs(transaction.amount)
    cost_basis_per_unit = (
        (cost_basis_total / quantity) if quantity != E("0") else E("0")
    )

    repo = TaxLotRepository(session)
    lot = TaxLot(
        tenant_id=tenant_id,
        account_id=str(transaction.account_id),
        security_id=str(transaction.security_id)
        if transaction.security_id
        else None,
        purchase_transaction_id=str(transaction.id),
        quantity=quantity,
        remaining_quantity=quantity,
        cost_basis_total=cost_basis_total,
        cost_basis_per_unit=cost_basis_per_unit,
        currency_code=transaction.currency_code,
        acquired_at=transaction.occurred_at,
        cost_basis_method=CostBasisMethod.FIFO.value,
    )
    return await repo.add(lot)


async def match_sale_to_lots(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
    *,
    cost_basis_method: str = CostBasisMethod.FIFO.value,
) -> list[dict[str, Any]]:
    """Match a sell transaction against open lots and close them.

    Returns a list of lot-closure records::

        [
            {
                "lot_id": str,
                "quantity_sold": Decimal,
                "cost_basis_used": Decimal,
                "proceeds": Decimal,
                "realized_pl": Decimal,
            },
            ...
        ]

    The total ``proceeds`` + ``realized_pl`` across all returned records
    equals the realised P&L for the whole sale.

    Partial-sale handling
    ---------------------
    If a lot has more shares than needed, the lot is split: the original lot
    gets ``remaining_quantity`` reduced and the sold portion is recorded as
    a new closed lot.  This keeps the database model simple (no separate
    ``LotSplit`` table).
    """
    assert transaction.transaction_type == TransactionType.SALE
    repo = TaxLotRepository(session)

    sale_quantity = (
        abs(transaction.quantity) if transaction.quantity else E("0")
    )
    if sale_quantity <= E("0"):
        return []  # No quantity info — can't match

    security_id = (
        str(transaction.security_id) if transaction.security_id else None
    )
    if not security_id:
        return []

    open_lots = await repo.find_open_lots(
        tenant_id=tenant_id,
        account_id=str(transaction.account_id),
        security_id=security_id,
    )

    if cost_basis_method == CostBasisMethod.LIFO.value:
        open_lots = list(reversed(open_lots))
    # FIFO is the default (already sorted ascending)

    closure_records: list[dict[str, Any]] = []
    remaining_to_sell = sale_quantity

    # Calculate proceeds per unit from the transaction
    total_proceeds = abs(transaction.amount)
    sale_qty_from_txn = (
        abs(transaction.quantity)
        if transaction.quantity and transaction.quantity != E("0")
        else sale_quantity
    )
    proceeds_per_unit = (
        total_proceeds / sale_qty_from_txn
        if sale_qty_from_txn != E("0")
        else E("0")
    )

    for lot in open_lots:
        if remaining_to_sell <= E("0"):
            break

        available = lot.remaining_quantity
        if available <= E("0"):
            continue

        qty_sold = min(available, remaining_to_sell)
        cost_used = qty_sold * lot.cost_basis_per_unit

        # Proceeds for this portion
        portion_proceeds = qty_sold * proceeds_per_unit

        realized_pl = portion_proceeds - cost_used

        # Update the existing lot
        lot.remaining_quantity = available - qty_sold

        if lot.remaining_quantity == E("0"):
            # Fully closed
            lot.closed_at = transaction.occurred_at
            lot.sale_transaction_id = str(transaction.id)
            lot.realized_pl = realized_pl
            lot.realized_pl_currency = transaction.currency_code

        remaining_to_sell -= qty_sold

        closure_records.append(
            {
                "lot_id": str(lot.id),
                "quantity_sold": qty_sold,
                "cost_basis_used": cost_used,
                "proceeds": portion_proceeds,
                "realized_pl": realized_pl,
            }
        )

        if remaining_to_sell > E("0"):
            # Save partial update
            await repo.update(lot)

    if remaining_to_sell > E("0"):
        # Not enough open lots — this can happen with uncovered short
        # sales or data that wasn't ingested yet.  Log a warning and
        # return what we have.
        closure_records.append(
            {
                "lot_id": None,
                "quantity_sold": sale_quantity - remaining_to_sell,
                "cost_basis_used": E("0"),
                "proceeds": E("0"),
                "realized_pl": E("0"),
                "unmatched_quantity": remaining_to_sell,
            }
        )

    return closure_records


async def compute_unrealized_pl(
    lots: list[TaxLot],
    current_price: Decimal,
    current_price_currency: str | None = None,
) -> list[dict[str, Any]]:
    """Compute unrealised P&L for a list of open tax lots.

    Each lot is valued at ``remaining_quantity * current_price`` and
    compared against ``remaining_quantity * cost_basis_per_unit``.
    """
    results: list[dict[str, Any]] = []
    for lot in lots:
        if lot.is_open() and lot.remaining_quantity > E("0"):
            current_value = lot.remaining_quantity * current_price
            remaining_cost = lot.remaining_quantity * lot.cost_basis_per_unit
            unrealized_pl = current_value - remaining_cost
            unrealized_pl_pct = (
                (unrealized_pl / remaining_cost * E("100"))
                if remaining_cost != E("0")
                else None
            )
            results.append(
                {
                    "lot_id": str(lot.id),
                    "remaining_quantity": lot.remaining_quantity,
                    "cost_basis_remaining": remaining_cost,
                    "current_value": current_value,
                    "unrealized_pl": unrealized_pl,
                    "unrealized_pl_pct": unrealized_pl_pct,
                    "currency_code": current_price_currency
                    or lot.currency_code,
                }
            )
    return results


async def apply_quantity_event_to_lots(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
    ratio: Decimal,
) -> dict[str, Any]:
    """Apply a provider-confirmed quantity ratio to open tax lots.

    A split changes units but not the total acquisition cost.  Updating the
    per-unit basis by the inverse ratio preserves that invariant while making
    the reconstructed lots agree with the post-event holding snapshot.
    Closed lots are historical and intentionally remain unchanged.
    """
    if ratio <= E("0") or not transaction.security_id:
        return {
            "action": "quantity_event_skipped",
            "reason": "invalid_ratio_or_missing_security",
        }

    repo = TaxLotRepository(session)
    open_lots = await repo.find_open_lots(
        tenant_id=tenant_id,
        account_id=str(transaction.account_id),
        security_id=str(transaction.security_id),
    )
    for lot in open_lots:
        lot.quantity *= ratio
        lot.remaining_quantity *= ratio
        lot.cost_basis_per_unit /= ratio
        await repo.update(lot)
    return {
        "action": "quantity_event_applied",
        "event_id": str(transaction.id),
        "ratio": str(ratio),
        "lots_adjusted": len(open_lots),
        "cost_basis_preserved": True,
    }


async def detect_and_adjust_wash_sales(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
    *,
    lookback_days: int = WASH_SALE_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Detect wash sales around a realised loss transaction.

    A wash sale occurs when you sell a security at a loss and purchase
    the same (or substantially identical) security within a 30-day window
    (30 days before or after the sale).

    When detected, the disallowed loss is added to the cost basis of the
    replacement lots, deferring the loss to the eventual sale of those
    replacement shares.

    Returns a list of wash sale adjustment records.
    """
    if transaction.transaction_type != TransactionType.SALE:
        return []

    # Only matters if there's a realised loss
    sale_quantity = (
        abs(transaction.quantity) if transaction.quantity else E("0")
    )
    if sale_quantity <= E("0"):
        return []

    repo = TaxLotRepository(session)
    security_id = (
        str(transaction.security_id) if transaction.security_id else None
    )
    if not security_id:
        return []

    # Find open lots for this security that were acquired in the
    # wash-sale window (30 days before or after this sale)
    window_start = transaction.occurred_at - timedelta(days=lookback_days)
    window_end = transaction.occurred_at + timedelta(days=lookback_days)

    # All lots purchased in the window (open or closed)
    all_lots = await repo.list(
        TaxLot.tenant_id == tenant_id,  # type: ignore[attr-defined]
        TaxLot.account_id == str(transaction.account_id),  # type: ignore[attr-defined]
        TaxLot.security_id == security_id,  # type: ignore[attr-defined]
        TaxLot.acquired_at >= window_start,  # type: ignore[attr-defined]
        TaxLot.acquired_at <= window_end,  # type: ignore[attr-defined]
        order_by=TaxLot.acquired_at.asc(),  # type: ignore[attr-defined]
    )

    adjustments: list[dict[str, Any]] = []

    # Find the loss on this sale by looking at the lots that were closed
    closed_lots = await repo.find_lots_for_transaction(
        tenant_id, str(transaction.id)
    )

    total_loss = E("0")
    for lot in closed_lots:
        if lot.realized_pl is not None and lot.realized_pl < E("0"):
            total_loss += abs(lot.realized_pl)

    if total_loss <= E("0"):
        return []  # No loss — no wash sale adjustment needed

    # Find replacement lots (open lots purchased within window)
    replacement_lots = [
        lot
        for lot in all_lots
        if lot.is_open() and lot.purchase_transaction_id != str(transaction.id)
    ]

    remaining_loss = total_loss
    for replacement in replacement_lots:
        if remaining_loss <= E("0"):
            break

        # Adjust the replacement lot's cost basis by the disallowed loss
        disallowed = min(remaining_loss, replacement.cost_basis_total)

        replacement.cost_basis_total += disallowed
        replacement.cost_basis_per_unit = (
            replacement.cost_basis_total / replacement.quantity
            if replacement.quantity > E("0")
            else E("0")
        )
        replacement.has_wash_sale_adjustment = True
        replacement.disallowed_loss = disallowed

        adjustments.append(
            {
                "replacement_lot_id": str(replacement.id),
                "disallowed_loss": disallowed,
                "remaining_loss_carried_forward": remaining_loss - disallowed,
            }
        )
        remaining_loss -= disallowed

    return adjustments


async def process_transaction(
    session: AsyncSession,
    tenant_id: str,
    transaction: Transaction,
    *,
    cost_basis_method: str = CostBasisMethod.FIFO.value,
    paired_transfer: Transaction | None = None,
) -> list[dict[str, Any]]:
    """Process a single transaction and update tax lots accordingly.

    - ``PURCHASE`` transactions create new open tax lots.
    - ``SALE`` transactions match against open lots and compute realised P&L,
      then run wash sale detection.

    Returns a list of action records describing what was done.
    """
    actions: list[dict[str, Any]] = []
    txn_type = transaction.transaction_type

    if txn_type == TransactionType.PURCHASE and transaction.security_id:
        lot = await create_tax_lots_for_purchase(
            session, tenant_id, transaction
        )
        actions.append(
            {
                "action": "lot_created",
                "lot_id": str(lot.id),
                "quantity": str(lot.quantity),
            }
        )
    elif txn_type == TransactionType.SALE and transaction.security_id:
        closures = await match_sale_to_lots(
            session,
            tenant_id,
            transaction,
            cost_basis_method=cost_basis_method,
        )
        matched_qty = sum(c.get("quantity_sold", E("0")) for c in closures)
        total_pl = sum(c.get("realized_pl", E("0")) for c in closures)
        actions.append(
            {
                "action": "lots_matched",
                "lots_closed": len(closures),
                "total_quantity_matched": str(matched_qty),
                "total_realized_pl": str(total_pl),
            }
        )

        # Check for wash sales
        wash_adjustments = await detect_and_adjust_wash_sales(
            session, tenant_id, transaction
        )
        if wash_adjustments:
            actions.append(
                {
                    "action": "wash_sale_adjustment",
                    "adjustments": wash_adjustments,
                }
            )
    elif (
        txn_type == TransactionType.TRANSFER
        and transaction.security_id
        and paired_transfer is not None
    ):
        actions.append(
            await transfer_lots_to_destination(
                session,
                tenant_id,
                transaction,
                paired_transfer,
            )
        )
    elif txn_type in {
        TransactionType.SPLIT,
        TransactionType.ADJUSTMENT,
        "corporate_action",
    }:
        ratio = quantity_event_ratio(transaction.provider_metadata_contract)
        if ratio is None:
            actions.append(
                {
                    "action": "quantity_event_skipped",
                    "event_id": str(transaction.id),
                    "reason": "missing_positive_ratio",
                }
            )
        else:
            actions.append(
                await apply_quantity_event_to_lots(
                    session,
                    tenant_id,
                    transaction,
                    ratio,
                )
            )

    return actions


async def get_tax_lot_summary(
    session: AsyncSession,
    tenant_id: str,
    *,
    account_id: str | None = None,
    security_id: str | None = None,
    include_closed: bool = True,
    visible_account_ids: Any | None = None,
) -> dict[str, Any]:
    """Get a summary of tax lots for a tenant.

    Returns counts and totals for open and closed lots.  When
    ``visible_account_ids`` (a SQL predicate over ``Account.id``) is
    given, only lots on those accounts are included (account-scope
    scoping).
    """

    repo = TaxLotRepository(session)
    conditions: list[Any] = [TaxLot.tenant_id == tenant_id]  # type: ignore[attr-defined]
    if visible_account_ids is not None:
        conditions.append(TaxLot.account_id.in_(visible_account_ids))  # type: ignore[attr-defined]

    if account_id:
        conditions.append(TaxLot.account_id == account_id)  # type: ignore[attr-defined]
    if security_id:
        conditions.append(TaxLot.security_id == security_id)  # type: ignore[attr-defined]

    lots = await repo.list(*conditions)

    open_lots = [lot for lot in lots if lot.is_open()]
    closed_lots = (
        [lot for lot in lots if not lot.is_open()] if include_closed else []
    )

    total_cost = sum(
        (lot.remaining_quantity * lot.cost_basis_per_unit) for lot in open_lots
    )
    total_realized_pl = sum((lot.realized_pl or E("0")) for lot in closed_lots)

    return {
        "total_lots": len(lots),
        "open_lots": len(open_lots),
        "closed_lots": len(closed_lots),
        "open_cost_basis": total_cost,
        "total_realized_pl": total_realized_pl,
        "wash_sale_adjusted_lots": sum(
            1 for lot in lots if lot.has_wash_sale_adjustment
        ),
    }


async def compute_all_tax_lots(
    session: AsyncSession,
    tenant_id: str,
    *,
    cost_basis_method: str = CostBasisMethod.FIFO.value,
) -> dict[str, Any]:
    """Recompute all tax lots for a tenant from scratch.

    Scans all purchase/sale transactions in order, creates tax lots,
    matches sales to lots, and detects wash sales.

    This is the full reconciliation endpoint.
    """
    from sqlalchemy import select

    # Keep the invariant in the service as well as in the HTTP endpoint:
    # direct callers must not append a second generation of lots.
    repo = TaxLotRepository(session)
    await repo.delete_for_tenant(tenant_id)

    # Get all transactions for this tenant ordered by occurred_at
    stmt = (
        select(Transaction)
        .where(
            Transaction.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Transaction.transaction_type.in_(
                [  # type: ignore[attr-defined]
                    TransactionType.PURCHASE.value,
                    TransactionType.SALE.value,
                    TransactionType.SPLIT.value,
                    TransactionType.ADJUSTMENT.value,
                    "corporate_action",
                    TransactionType.TRANSFER.value,
                ]
            ),
            Transaction.security_id.isnot(None),  # type: ignore[attr-defined]
        )
        .order_by(  # type: ignore[attr-defined]
            Transaction.occurred_at.asc(), Transaction.id.asc()
        )
    )
    result = await session.execute(stmt)
    transactions: list[Transaction] = list(result.scalars().all())  # type: ignore[assignment]
    transfer_pairs = pair_security_transfer_legs(transactions)

    stats = {
        "transactions_processed": 0,
        "lots_created": 0,
        "lots_closed": 0,
        "wash_sale_adjustments": 0,
        "quantity_events_processed": 0,
        "quantity_lots_adjusted": 0,
        "quantity_events_skipped": 0,
        "transfer_events_processed": 0,
        "transfer_lots_moved": 0,
        "transfer_basis_gaps": 0,
        "transfer_events_skipped": 0,
        "total_realized_pl": E("0"),
    }

    for txn in transactions:
        actions = await process_transaction(
            session,
            tenant_id,
            txn,
            cost_basis_method=cost_basis_method,
            paired_transfer=transfer_pairs.get(str(txn.id)),
        )
        stats["transactions_processed"] += 1

        for action in actions:
            if action.get("action") == "lot_created":
                stats["lots_created"] += 1
            elif action.get("action") == "lots_matched":
                stats["lots_closed"] += action.get("lots_closed", 0)
                pl = Decimal(action.get("total_realized_pl", "0"))
                stats["total_realized_pl"] += pl
            elif action.get("action") == "wash_sale_adjustment":
                stats["wash_sale_adjustments"] += 1
            elif action.get("action") == "quantity_event_applied":
                stats["quantity_events_processed"] += 1
                stats["quantity_lots_adjusted"] += int(
                    action.get("lots_adjusted", 0)
                )
            elif action.get("action") == "quantity_event_skipped":
                stats["quantity_events_skipped"] += 1
            elif action.get("action") == "transfer_basis_moved":
                stats["transfer_events_processed"] += 1
                stats["transfer_lots_moved"] += int(
                    action.get("lots_created", 0)
                )
            elif action.get("action") == "transfer_basis_gap":
                stats["transfer_events_processed"] += 1
                stats["transfer_basis_gaps"] += 1
            elif action.get("action") in {
                "transfer_basis_skipped",
                "transfer_basis_already_applied",
            }:
                stats["transfer_events_skipped"] += 1

    return stats
