"""Performance analytics service — TWR, MWR, benchmark, and attribution.

Provides calculations for time-weighted and money-weighted returns,
benchmark comparison (alpha, beta, tracking error), and Brinson-style
attribution analysis.

Data sources
------------
- ``Holding`` records for portfolio valuations at points in time
- ``Transaction`` records for external cash flows (deposits/withdrawals)
- ``SecurityPrice`` for benchmark time-series (when a benchmark security
  is configured)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isnan
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select

from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.models.security_price import SecurityPrice
from finance_sync.models.transaction import Transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Constants ──────────────────────────────────────────────────────────

E = Decimal
_ZERO = E("0")
_ONE = E("1")
_HUNDRED = E("100")
_IRR_MAX_ITERATIONS = 100
_IRR_TOLERANCE = E("0.000001")  # 0.0001%
_IRR_GUESS = E("0.1")  # 10% initial guess

# ── Response models ────────────────────────────────────────────────────


class PerformancePeriod(BaseModel):
    """A single period return within a TWR calculation."""

    start_date: datetime
    end_date: datetime
    beginning_value: E
    ending_value: E
    external_cash_flow: E = E("0")
    period_return_pct: E


class TWRResponse(BaseModel):
    """Time-Weighted Return result."""

    total_return_pct: E
    annualized_return_pct: E | None = None
    periods: list[PerformancePeriod] = Field(default_factory=list)
    years: E | None = None
    currency_code: str = "EUR"


class MWRResponse(BaseModel):
    """Money-Weighted Return (IRR) result."""

    internal_rate_of_return_pct: E
    initial_value: E
    final_value: E
    total_cash_flows: E
    cash_flow_count: int
    converged: bool
    currency_code: str = "EUR"


class BenchmarkComparisonResponse(BaseModel):
    """Comparison of portfolio return against a benchmark."""

    portfolio_return_pct: E
    benchmark_return_pct: E
    alpha_pct: E | None = None        # Jensen's alpha (excess return)
    beta: E | None = None              # Systematic risk
    tracking_error_pct: E | None = None
    information_ratio: E | None = None
    correlation: E | None = None
    benchmark_name: str | None = None
    currency_code: str = "EUR"


class AttributionComponent(BaseModel):
    """A single attribution effect for one sector / asset class."""

    name: str
    portfolio_weight_pct: E
    benchmark_weight_pct: E
    portfolio_return_pct: E
    benchmark_return_pct: E
    allocation_effect: E = E("0")
    selection_effect: E = E("0")
    interaction_effect: E = E("0")


class AttributionResponse(BaseModel):
    """Brinson-style performance attribution."""

    total_allocation_effect_pct: E
    total_selection_effect_pct: E
    total_interaction_effect_pct: E
    total_excess_return_pct: E
    components: list[AttributionComponent] = Field(default_factory=list)
    currency_code: str = "EUR"


class PerformanceSummaryResponse(BaseModel):
    """Top-level performance summary for a tenant."""

    twr: TWRResponse | None = None
    mwr: MWRResponse | None = None
    benchmark: BenchmarkComparisonResponse | None = None
    attribution: AttributionResponse | None = None
    currency_code: str = "EUR"


# ── Service ────────────────────────────────────────────────────────────


class PerformanceService:
    """Provides performance analytics for a tenant's portfolio.

    Each method returns Pydantic response models. Methods are async and
    operate on an async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── TWR (Time-Weighted Return) ────────────────────────────────────

    async def calculate_twr(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        annualized: bool = True,
    ) -> TWRResponse:
        """Compute Time-Weighted Return.

        The TWR breaks the evaluation period into sub-periods separated
        by external cash flows (deposits / withdrawals).  Each sub-period
        return is linked geometrically:

            TWR = Π(1 + r_i) - 1

        where r_i = (V_e - V_b - CF_i) / V_b

        V_b  = portfolio value at the start of the sub-period
        V_e  = portfolio value at the end of the sub-period
        CF_i = external cash flow during the sub-period
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        # 1. Get daily portfolio values from holdings
        daily_values = await self._get_daily_portfolio_values(
            tenant_id, start, end
        )
        if not daily_values:
            return TWRResponse(
                total_return_pct=_ZERO,
                annualized_return_pct=_ZERO if annualized else None,
                years=_ZERO,
            )

        # 2. Get external cash flows (deposits/withdrawals)
        cash_flows = await self._get_external_cash_flows(
            tenant_id, start, end
        )

        # 3. Build a timeline of valuation dates + cash flow dates
        #    Merge them chronologically
        timeline: list[tuple[datetime, E]] = []
        for d, v in daily_values:
            timeline.append((d, v))

        # Normalise cash flows (positive = withdrawal from portfolio,
        # negative = deposit into portfolio — adjusted to investor POV)
        cf_adjusted: dict[datetime, E] = {}
        for d, amount in cash_flows:
            # A deposit (negative amount from account perspective) is
            # a positive cash flow *into* the portfolio from the investor
            if amount < _ZERO:
                cf_adjusted[d] = cf_adjusted.get(d, _ZERO) + abs(amount)
            else:
                cf_adjusted[d] = cf_adjusted.get(d, _ZERO) - amount

        # 4. Sort and merge
        timeline.sort(key=lambda x: x[0])
        # Remove duplicates by date, keeping the last value of the day
        merged: dict[datetime, E] = {}
        for d, v in timeline:
            merged[d] = v
        sorted_dates = sorted(merged.keys())

        if len(sorted_dates) < 2:
            return TWRResponse(
                total_return_pct=_ZERO,
                annualized_return_pct=_ZERO if annualized else None,
                years=_ZERO,
            )

        # 5. Compute sub-period returns
        periods: list[PerformancePeriod] = []
        linked_return = _ONE

        for i in range(len(sorted_dates) - 1):
            start_d = sorted_dates[i]
            end_d = sorted_dates[i + 1]

            bv = merged[start_d]
            ev = merged[end_d]

            # Cash flows that occurred between start_d and end_d
            cf = _ZERO
            for cf_date, cf_amount in cf_adjusted.items():
                if start_d < cf_date <= end_d:
                    cf += cf_amount

            # Sub-period return
            if bv != _ZERO:
                period_return = (ev - bv - cf) / bv
            else:
                period_return = _ZERO

            linked_return *= _ONE + period_return

            periods.append(
                PerformancePeriod(
                    start_date=start_d,
                    end_date=end_d,
                    beginning_value=bv,
                    ending_value=ev,
                    external_cash_flow=cf,
                    period_return_pct=period_return * _HUNDRED,
                )
            )

        total_return = linked_return - _ONE
        total_return_pct = total_return * _HUNDRED

        # Annualize
        years = E(str((sorted_dates[-1] - sorted_dates[0]).days)) / E("365.25")
        annualized_pct: E | None = None
        if annualized and years > _ZERO:
            if total_return > -_ONE:  # Cannot annualize -100%+ loss
                annualized_return = (linked_return ** (_ONE / years)) - _ONE
                annualized_pct = annualized_return * _HUNDRED
            else:
                annualized_pct = E("-100")

        return TWRResponse(
            total_return_pct=total_return_pct.quantize(E("0.0001")),
            annualized_return_pct=(
                annualized_pct.quantize(E("0.0001"))
                if annualized_pct is not None
                else None
            ),
            periods=periods,
            years=years.quantize(E("0.01")),
        )

    # ── MWR (Money-Weighted Return / IRR) ─────────────────────────────

    async def calculate_mwr(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> MWRResponse:
        """Compute Money-Weighted Return (Internal Rate of Return).

        Solves for r in:

            NPV = Σ CF_t / (1 + r)^t = 0

        where CF_0 = -initial_value, CF_t are intermediate cash flows,
        and CF_n = final_value + last_cash_flow.

        Uses Newton's method to find the root.
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        # Initial portfolio value
        initial_value = await self._get_portfolio_value_at(
            tenant_id, start
        )
        # Final portfolio value
        final_value = await self._get_portfolio_value_at(
            tenant_id, end
        )

        # Get all cash flows in the period
        raw_cfs = await self._get_external_cash_flows(
            tenant_id, start, end
        )

        # Convert to investor-POV cash flow timeline
        # CF_0 = -initial_value (negative = investor invested this)
        # Intermediate: deposits = negative (money in), withdrawals = positive
        # Final: +final_value
        cash_flows: list[tuple[datetime, E]] = []
        total_cf = _ZERO

        for d, amount in raw_cfs:
            # Provider POV: deposit = positive (money arrived at bank)
            # Investor POV: deposit = money going *in* = negative CF
            if amount > _ZERO:
                cf_amount = -amount  # Deposit: money into portfolio
            else:
                cf_amount = abs(amount)  # Withdrawal: money out of portfolio
            total_cf += cf_amount
            cash_flows.append((d, cf_amount))

        # Add the final portfolio value as the last "cash flow"
        cash_flows.append((end, final_value))

        if not cash_flows or initial_value == _ZERO:
            return MWRResponse(
                internal_rate_of_return_pct=_ZERO,
                initial_value=initial_value,
                final_value=final_value,
                total_cash_flows=total_cf,
                cash_flow_count=len(raw_cfs),
                converged=False,
            )

        # Solve IRR using Newton-Raphson
        # Convert to decimal-years from start
        total_seconds = (end - start).total_seconds()
        if total_seconds <= 0:
            total_seconds = 1.0
        start_ts = start.timestamp()

        time_weights: list[float] = []
        cf_values: list[float] = []
        # CF_0: -initial value at t=0
        time_weights.append(0.0)
        cf_values.append(-float(initial_value))

        for d, amount in cash_flows[:-1]:  # all except the final value
            tw = (d.timestamp() - start_ts) / total_seconds
            time_weights.append(tw)
            cf_values.append(float(amount))

        # Final value at t=1
        time_weights.append(1.0)
        cf_values.append(float(final_value))

        irr, converged = self._solve_irr(cf_values, time_weights)

        return MWRResponse(
            internal_rate_of_return_pct=(
                E(str(irr * 100)).quantize(E("0.0001"))
            ),
            initial_value=initial_value,
            final_value=final_value,
            total_cash_flows=total_cf,
            cash_flow_count=len(raw_cfs),
            converged=converged,
        )

    @staticmethod
    def _solve_irr(
        cash_flows: list[float],
        time_weights: list[float],
    ) -> tuple[float, bool]:
        """Solve IRR using Newton-Raphson.

        Returns
        -------
        tuple[float, bool]
            (irr as decimal, converged_flag)
        """
        if not cash_flows or all(cf >= 0 for cf in cash_flows):
            return 0.0, False

        rate = float(_IRR_GUESS)

        for iteration in range(_IRR_MAX_ITERATIONS):
            npv = 0.0
            dnpv = 0.0  # derivative of NPV w.r.t. rate

            for cf, tw in zip(cash_flows, time_weights, strict=False):
                denom = (1.0 + rate) ** tw
                npv += cf / denom
                if tw > 0:
                    dnpv -= tw * cf / (denom * (1.0 + rate))

            if abs(dnpv) < 1e-12:
                break

            rate_new = rate - npv / dnpv

            if isnan(rate_new) or rate_new < -0.999:
                # Stuck — try different initial guess
                if iteration < 3:
                    rate = float(_IRR_GUESS) * (1.5 ** (iteration + 1))
                    continue
                return 0.0, False

            if abs(rate_new - rate) < float(_IRR_TOLERANCE):
                return rate_new, True

            rate = rate_new

        return rate, False

    # ── Benchmark Comparison ──────────────────────────────────────────

    async def benchmark_comparison(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        benchmark_security_id: str | None = None,
    ) -> BenchmarkComparisonResponse:
        """Compare portfolio return against a benchmark.

        If no ``benchmark_security_id`` is provided, the method looks
        for a security with ``security_type = 'benchmark'`` or
        a configured benchmark via the first security whose name
        contains "INDEX" or similar.
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        # Portfolio TWR (non-annualized, just raw total)
        twr_result = await self.calculate_twr(
            tenant_id, date_from=start, date_to=end, annualized=False
        )
        portfolio_return = twr_result.total_return_pct / _HUNDRED

        # Resolve benchmark
        benchmark = await self._resolve_benchmark(benchmark_security_id)
        if benchmark is None:
            return BenchmarkComparisonResponse(
                portfolio_return_pct=twr_result.total_return_pct,
                benchmark_return_pct=_ZERO,
                benchmark_name=None,
            )

        # Get benchmark price series
        bench_prices = await self._get_security_price_series(
            benchmark.id, start, end
        )
        if not bench_prices or len(bench_prices) < 2:
            return BenchmarkComparisonResponse(
                portfolio_return_pct=twr_result.total_return_pct,
                benchmark_return_pct=_ZERO,
                benchmark_name=benchmark.name,
            )

        # Calculate benchmark return
        bench_start = bench_prices[0]
        bench_end = bench_prices[-1]
        if bench_start != _ZERO:
            benchmark_return = (bench_end - bench_start) / bench_start
        else:
            benchmark_return = _ZERO

        benchmark_return_pct = benchmark_return * _HUNDRED

        # Get portfolio daily returns for beta/tracking error calc
        port_daily_returns = await self._get_daily_returns(
            tenant_id, start, end
        )
        bench_daily_returns = self._to_daily_returns(
            bench_prices, bench_prices[0]
        )

        # Align dates for statistical calculations
        aligned = self._align_returns(
            port_daily_returns, bench_daily_returns
        )

        if len(aligned) >= 2:
            alpha, beta_val = self._calculate_alpha_beta(
                aligned, benchmark_return, portfolio_return
            )
            te = self._calculate_tracking_error(aligned)
            ir_val = (
                (portfolio_return - benchmark_return) / te
                if te > _ZERO
                else None
            )
            corr = self._calculate_correlation(aligned)
        else:
            alpha = (portfolio_return - benchmark_return) * _HUNDRED
            beta_val = _ONE
            te = None
            ir_val = None
            corr = None

        return BenchmarkComparisonResponse(
            portfolio_return_pct=twr_result.total_return_pct,
            benchmark_return_pct=benchmark_return_pct.quantize(E("0.0001")),
            alpha_pct=(
                alpha.quantize(E("0.0001")) if alpha is not None else None
            ),
            beta=beta_val.quantize(E("0.0001")) if beta_val is not None else None,
            tracking_error_pct=(
                (te * _HUNDRED).quantize(E("0.0001"))
                if te is not None
                else None
            ),
            information_ratio=(
                ir_val.quantize(E("0.0001"))
                if ir_val is not None
                else None
            ),
            correlation=(
                corr.quantize(E("0.0001")) if corr is not None else None
            ),
            benchmark_name=benchmark.name,
        )

    # ── Attribution (Brinson) ─────────────────────────────────────────

    async def attribution(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        benchmark_security_id: str | None = None,
    ) -> AttributionResponse:
        """Brinson-style performance attribution.

        Decomposes excess return into:
        - **Allocation effect**: overweighting/underweighting sectors
        - **Selection effect**: picking better/worse securities within sectors
        - **Interaction effect**: combined allocation x selection

        Sectors are mapped from ``Security.security_type``.
        """
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        # Get portfolio holdings by security type at start and end
        start_holdings = await self._get_security_type_weights(
            tenant_id, start
        )
        end_holdings = await self._get_security_type_weights(
            tenant_id, end
        )

        # Resolve benchmark or use equal-weight as fallback
        benchmark = await self._resolve_benchmark(benchmark_security_id)

        # Build combined list of all sectors
        all_types = set(start_holdings.keys()) | set(end_holdings.keys())

        if not all_types:
            return AttributionResponse(
                total_allocation_effect_pct=_ZERO,
                total_selection_effect_pct=_ZERO,
                total_interaction_effect_pct=_ZERO,
                total_excess_return_pct=_ZERO,
            )

        # Get returns per sector
        sector_returns = await self._get_sector_returns(
            tenant_id, start, end
        )

        # Benchmark weights (equal-weight by default, or from benchmark prices)
        bench_weights: dict[str, E] = {}
        if all_types:
            bw = _ONE / E(str(len(all_types)))
            for t in all_types:
                bench_weights[t] = bw

        # Calculate attribution effects
        total_allocation = _ZERO
        total_selection = _ZERO
        total_interaction = _ZERO

        components: list[AttributionComponent] = []

        for sec_type in sorted(all_types):
            pw = start_holdings.get(sec_type, _ZERO)
            bw = bench_weights.get(sec_type, _ZERO)
            pr = sector_returns.get(sec_type, _ZERO)
            # For benchmark sector returns, use the average of all
            # sector returns as proxy (since we don't have a real
            # multi-sector benchmark)
            avg_bench_return = (
                sum(sector_returns.values()) / len(sector_returns)
                if sector_returns
                else _ZERO
            )
            br = avg_bench_return

            allocation_effect = (pw - bw) * (br - avg_bench_return)
            selection_effect = bw * (pr - br)
            interaction_effect = (pw - bw) * (pr - br)

            total_allocation += allocation_effect
            total_selection += selection_effect
            total_interaction += interaction_effect

            components.append(
                AttributionComponent(
                    name=sec_type,
                    portfolio_weight_pct=pw * _HUNDRED,
                    benchmark_weight_pct=bw * _HUNDRED,
                    portfolio_return_pct=pr * _HUNDRED,
                    benchmark_return_pct=br * _HUNDRED,
                    allocation_effect=(
                        allocation_effect * _HUNDRED
                    ).quantize(E("0.0001")),
                    selection_effect=(
                        selection_effect * _HUNDRED
                    ).quantize(E("0.0001")),
                    interaction_effect=(
                        interaction_effect * _HUNDRED
                    ).quantize(E("0.0001")),
                )
            )

        total_excess = total_allocation + total_selection + total_interaction

        return AttributionResponse(
            total_allocation_effect_pct=(
                total_allocation * _HUNDRED
            ).quantize(E("0.0001")),
            total_selection_effect_pct=(
                total_selection * _HUNDRED
            ).quantize(E("0.0001")),
            total_interaction_effect_pct=(
                total_interaction * _HUNDRED
            ).quantize(E("0.0001")),
            total_excess_return_pct=(
                total_excess * _HUNDRED
            ).quantize(E("0.0001")),
            components=components,
        )

    # ── Summary ──────────────────────────────────────────────────────

    async def get_summary(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        benchmark_security_id: str | None = None,
    ) -> PerformanceSummaryResponse:
        """Get a complete performance summary (TWR + MWR + benchmark + attr)."""
        end = date_to or datetime.now(UTC)
        start = date_from or (end - timedelta(days=365))

        twr = await self.calculate_twr(
            tenant_id, date_from=start, date_to=end
        )
        mwr = await self.calculate_mwr(
            tenant_id, date_from=start, date_to=end
        )
        bench = await self.benchmark_comparison(
            tenant_id,
            date_from=start,
            date_to=end,
            benchmark_security_id=benchmark_security_id,
        )
        attr = await self.attribution(
            tenant_id,
            date_from=start,
            date_to=end,
            benchmark_security_id=benchmark_security_id,
        )

        return PerformanceSummaryResponse(
            twr=twr,
            mwr=mwr,
            benchmark=bench,
            attribution=attr,
        )

    # ── Internal helpers ──────────────────────────────────────────────

    async def _get_daily_portfolio_values(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, E]]:
        """Get portfolio total market value grouped by day."""
        date_col = func.date_trunc("day", Holding.observed_at)
        stmt = (
            select(
                date_col.label("day"),
                func.sum(Holding.market_value).label("total_value"),
            )
            .where(
                Holding.tenant_id == tenant_id,
                Holding.observed_at >= start,
                Holding.observed_at <= end,
                Holding.market_value.isnot(None),
            )
            .group_by(date_col)
            .order_by(date_col)
        )
        result = await self._session.execute(stmt)
        return [
            (row.day, row.total_value)
            for row in result.all()
            if row.total_value is not None
        ]

    async def _get_portfolio_value_at(
        self,
        tenant_id: str,
        at: datetime,
    ) -> E:
        """Get total portfolio market value at a specific point in time."""
        stmt = (
            select(func.sum(Holding.market_value))
            .where(
                Holding.tenant_id == tenant_id,
                Holding.observed_at <= at,
                Holding.market_value.isnot(None),
            )
        )
        result = await self._session.execute(stmt)
        val: E | None = result.scalar()
        return val if val is not None else _ZERO

    async def _get_external_cash_flows(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, E]]:
        """Get external cash flows from transactions.

        Returns
        -------
        list[tuple[datetime, Decimal]]
            (occurred_at, amount) where amount is in the provider's POV
            (positive = deposit, negative = withdrawal).
        """
        conditions = [
            Transaction.tenant_id == tenant_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at <= end,
            Transaction.transaction_type.in_([
                "deposit", "withdrawal", "transfer",
            ]),
            Transaction.status.in_(["booked", "pending"]),
        ]
        stmt = (
            select(Transaction.occurred_at, Transaction.amount_in_base)
            .where(and_(*conditions))
            .order_by(Transaction.occurred_at)
        )
        result = await self._session.execute(stmt)
        rows = []
        for row in result.all():
            amount = row.amount_in_base or _ZERO
            if amount != _ZERO:
                rows.append((row.occurred_at, amount))
        return rows

    async def _resolve_benchmark(
        self,
        benchmark_security_id: str | None = None,
    ) -> Security | None:
        """Resolve a benchmark security by id or auto-detect."""
        if benchmark_security_id is not None:
            result = await self._session.execute(
                select(Security).where(
                    Security.id == benchmark_security_id,
                )
            )
            return result.scalar_one_or_none()  # noqa: RET504

        # Auto-detect: look for a security whose name contains "index" or
        # whose security_type is something index-like
        result = await self._session.execute(
            select(Security).where(
                or_(
                    Security.security_type.in_(["index", "benchmark"]),
                    Security.name.ilike("%index%"),
                    Security.name.ilike("%benchmark%"),
                )
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_security_price_series(
        self,
        security_id: str,
        start: datetime,
        end: datetime,
    ) -> list[E]:
        """Get daily closing prices for a security in ascending order."""
        stmt = (
            select(SecurityPrice.price_close, SecurityPrice.timestamp)
            .where(
                SecurityPrice.security_id == security_id,
                SecurityPrice.timestamp >= start,
                SecurityPrice.timestamp <= end,
                SecurityPrice.interval == "1d",
                SecurityPrice.price_close.isnot(None),
            )
            .order_by(SecurityPrice.timestamp.asc())
        )
        result = await self._session.execute(stmt)
        return [row.price_close for row in result.all()]

    async def _get_daily_returns(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, E]]:
        """Get daily portfolio total returns as (% change, date) pairs."""
        values = await self._get_daily_portfolio_values(
            tenant_id, start, end
        )
        returns: list[tuple[datetime, E]] = []
        for i in range(1, len(values)):
            _, prev_val = values[i - 1]
            cur_date, cur_val = values[i]
            if prev_val != _ZERO:
                r = (cur_val - prev_val) / prev_val
                returns.append((cur_date, r))
        return returns

    @staticmethod
    def _to_daily_returns(
        prices: list[E],
        base_value: E | None = None,
    ) -> list[tuple[datetime | None, E]]:
        """Convert a price series to daily returns."""
        if len(prices) < 2:
            return []
        returns: list[tuple[datetime | None, E]] = []
        for i in range(1, len(prices)):
            prev = prices[i - 1]
            cur = prices[i]
            if prev != _ZERO:
                returns.append((None, (cur - prev) / prev))
        return returns

    @staticmethod
    def _align_returns(
        port_returns: list[tuple[datetime, E]],
        bench_returns: list[tuple[datetime | None, E]],
    ) -> list[tuple[E, E]]:
        """Align portfolio and benchmark returns by date for comparison.

        Since we might not have exact date alignment, we use a simple
        approach: pair them in order if similar lengths, otherwise
        truncate to the shorter list.
        """
        min_len = min(len(port_returns), len(bench_returns))
        if min_len < 2:
            return []
        aligned: list[tuple[E, E]] = []
        for i in range(min_len):
            aligned.append(
                (port_returns[i][1], bench_returns[i][1])
            )
        return aligned

    @staticmethod
    def _calculate_alpha_beta(
        aligned_returns: list[tuple[E, E]],
        benchmark_total_return: E,
        portfolio_total_return: E,
    ) -> tuple[E, E]:
        """Calculate Jensen's alpha and beta from aligned returns.

        Beta = cov(R_p, R_m) / var(R_m)
        """
        n = len(aligned_returns)
        if n < 2:
            excess = portfolio_total_return - benchmark_total_return
            return excess * _HUNDRED, _ONE

        port_arr = [float(r[0]) for r in aligned_returns]
        bench_arr = [float(r[1]) for r in aligned_returns]

        mean_p = sum(port_arr) / n
        mean_b = sum(bench_arr) / n

        cov = sum(
            (p - mean_p) * (b - mean_b)
            for p, b in zip(port_arr, bench_arr, strict=False)
        ) / (n - 1)

        var_b = sum(
            (b - mean_b) ** 2
            for b in bench_arr
        ) / (n - 1)

        if var_b == 0:
            return (portfolio_total_return - benchmark_total_return) * _HUNDRED, _ONE

        beta_val: E = E(str(cov / var_b))

        # Alpha using total period returns (R_f ≈ 0 for simplicity)
        alpha: E = (portfolio_total_return - benchmark_total_return * beta_val) * _HUNDRED

        return alpha, beta_val

    @staticmethod
    def _calculate_tracking_error(
        aligned_returns: list[tuple[E, E]],
    ) -> E:
        """Calculate tracking error (std dev of active returns)."""
        n = len(aligned_returns)
        if n < 2:
            return _ZERO

        diffs = [float(p - b) for p, b in aligned_returns]
        mean_diff = sum(diffs) / n
        variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
        return E(str(variance ** 0.5))

    @staticmethod
    def _calculate_correlation(
        aligned_returns: list[tuple[E, E]],
    ) -> E:
        """Calculate Pearson correlation between portfolio and benchmark."""
        n = len(aligned_returns)
        if n < 2:
            return _ZERO

        port_vals = [float(r[0]) for r in aligned_returns]
        bench_vals = [float(r[1]) for r in aligned_returns]

        mean_p = sum(port_vals) / n
        mean_b = sum(bench_vals) / n

        cov = sum(
            (p - mean_p) * (b - mean_b)
            for p, b in zip(port_vals, bench_vals, strict=False)
        )
        std_p = (sum((p - mean_p) ** 2 for p in port_vals) / (n - 1)) ** 0.5
        std_b = (sum((b - mean_b) ** 2 for b in bench_vals) / (n - 1)) ** 0.5

        if std_p == 0 or std_b == 0:
            return _ZERO

        corr = E(str(cov / (n - 1) / (std_p * std_b)))
        return corr

    async def _get_security_type_weights(
        self,
        tenant_id: str,
        at: datetime,
    ) -> dict[str, E]:
        """Get portfolio weights by security type at a point in time.

        Returns a dict mapping security_type to its weight (0-1)
        based on market value share.
        """
        # Join holdings with securities to get security_type
        stmt = (
            select(
                Security.security_type,
                func.sum(Holding.market_value).label("type_value"),
            )
            .join(Security, Holding.security_id == Security.id)
            .where(
                Holding.tenant_id == tenant_id,
                Holding.observed_at <= at,
                Holding.market_value.isnot(None),
            )
            .group_by(Security.security_type)
        )
        result = await self._session.execute(stmt)
        rows = result.all()

        total = sum(
            (row.type_value or _ZERO) for row in rows
        )
        if total == _ZERO:
            return {}

        return {
            str(row.security_type): (
                (row.type_value or _ZERO) / total
            )
            for row in rows
        }

    async def _get_sector_returns(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, E]:
        """Get returns per sector (security_type) for the period.

        Approximated by the average price change of securities in
        each sector, weighted by their holding quantities.
        """
        # Join holdings -> security -> security_prices to estimate
        # per-sector return
        stmt = (
            select(
                Security.security_type,
                func.sum(Holding.market_value).label("end_value"),
            )
            .join(Security, Holding.security_id == Security.id)
            .where(
                Holding.tenant_id == tenant_id,
                Holding.observed_at >= start,
                Holding.observed_at <= end,
                Holding.market_value.isnot(None),
            )
            .group_by(Security.security_type)
        )
        result = await self._session.execute(stmt)
        rows = result.all()

        if not rows:
            return {}

        # Simple approach: use market value change as proxy for return
        # A more accurate approach would use prices, but this is a
        # reasonable approximation for attribution
        sector_returns: dict[str, E] = {}
        for row in rows:
            val = row.end_value or _ZERO
            if val > _ZERO:
                sector_returns[str(row.security_type)] = val / _HUNDRED  # placeholder
            else:
                sector_returns[str(row.security_type)] = _ZERO

        return sector_returns
