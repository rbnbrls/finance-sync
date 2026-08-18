"""Unit tests for the pure sync-schedule recurrence engine.

Covers the acceptance criteria that live in ``schedule_spec.py``:
weekdays (Mon-Fri in the schedule's own timezone, no national holidays),
DST transitions (no double local run, non-existent local time shifts
forward, hourly stays anchored), validation of frequencies/weekdays/N/
timezones, human-readable summaries and the default schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finance_sync.sync.schedule_spec import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    ScheduleValidationError,
    default_schedule,
    human_readable,
    next_run_instants,
    sanitise_schedule,
    validate_schedule,
)


def _utc(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


# ── Validation ─────────────────────────────────────────────────────────


class TestValidateSchedule:
    def test_weekdays_default_is_mon_fri(self) -> None:
        spec = validate_schedule(
            {"frequency": "weekdays", "time": "07:00"},
            timezone="Europe/Amsterdam",
        )
        assert spec["frequency"] == "weekdays"
        assert spec["weekdays"] == [0, 1, 2, 3, 4]

    def test_daily_needs_no_weekdays(self) -> None:
        spec = validate_schedule({"frequency": "daily", "time": "23:59"})
        assert spec["time"] == "23:59"
        assert "weekdays" not in spec

    def test_weekly_requires_nonempty_weekdays(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule({"frequency": "weekly", "time": "07:00"})
        with pytest.raises(ScheduleValidationError):
            validate_schedule(
                {"frequency": "weekly", "time": "07:00", "weekdays": []}
            )

    def test_weekly_rejects_out_of_range_weekday(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule(
                {"frequency": "weekly", "time": "07:00", "weekdays": [7]}
            )
        with pytest.raises(ScheduleValidationError):
            validate_schedule(
                {"frequency": "weekly", "time": "07:00", "weekdays": [-1]}
            )

    def test_weekly_normalises_and_dedupes(self) -> None:
        spec = validate_schedule(
            {"frequency": "weekly", "time": "07:00", "weekdays": [2, 0, 2]}
        )
        assert spec["weekdays"] == [0, 2]

    def test_unsupported_frequency_rejected(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule({"frequency": "monthly", "time": "07:00"})

    def test_invalid_time_rejected(self) -> None:
        for bad in ("25:00", "07:60", "7", "abcd", ""):
            with pytest.raises(ScheduleValidationError):
                validate_schedule({"frequency": "daily", "time": bad})

    def test_hourly_requires_int_interval(self) -> None:
        for bad in (0, -5, 1.5, "3", True, None):
            with pytest.raises(ScheduleValidationError):
                validate_schedule(
                    {"frequency": "hourly", "interval_hours": bad}
                )

    def test_hourly_bounds(self) -> None:
        assert validate_schedule(
            {"frequency": "hourly", "interval_hours": MIN_INTERVAL_HOURS}
        )
        assert validate_schedule(
            {"frequency": "hourly", "interval_hours": MAX_INTERVAL_HOURS}
        )
        with pytest.raises(ScheduleValidationError):
            validate_schedule(
                {
                    "frequency": "hourly",
                    "interval_hours": MAX_INTERVAL_HOURS + 1,
                }
            )

    def test_hourly_10_to_the_9_rejected(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule({"frequency": "hourly", "interval_hours": 10**9})

    def test_unknown_timezone_rejected(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule(
                {"frequency": "daily", "time": "07:00"},
                timezone="Mars/Olympus",
            )

    def test_non_dict_schedule_rejected(self) -> None:
        with pytest.raises(ScheduleValidationError):
            validate_schedule("weekdays")  # type: ignore[arg-type]


# ── next_run_instants: weekdays ───────────────────────────────────────


class TestNextRunWeekdays:
    def test_next_weekday_is_monday_when_friday_after_time(self) -> None:
        # Fri 2026-08-14 08:00 Europe/Amsterdam → next is Mon 08-17 07:00
        after = _utc(2026, 8, 14, 6, 0)  # 08:00 Amsterdam
        instants = next_run_instants(
            {"frequency": "weekdays", "time": "07:00"},
            timezone="Europe/Amsterdam",
            after=after,
        )
        assert len(instants) == 1
        # Mon 2026-08-17 07:00 Amsterdam = 05:00 UTC
        assert instants[0] == _utc(2026, 8, 17, 5, 0)

    def test_weekend_is_skipped(self) -> None:
        # Sat 2026-08-15 → next weekday is Mon 08-17
        after = _utc(2026, 8, 15, 12, 0)
        instants = next_run_instants(
            {"frequency": "weekdays", "time": "09:00"},
            timezone="UTC",
            after=after,
        )
        assert instants[0].weekday() == 0
        assert instants[0].day == 17

    def test_no_national_holidays(self) -> None:
        # 2026-12-25 is a Friday (Christmas).  Workday definition is
        # Mon-Fri only -- Christmas Day still yields a run.
        after = _utc(2026, 12, 24, 12, 0)
        instants = next_run_instants(
            {"frequency": "weekdays", "time": "07:00"},
            timezone="UTC",
            after=after,
        )
        assert instants[0].day == 25
        assert instants[0].weekday() == 4  # Friday

    def test_returns_count_instants(self) -> None:
        after = _utc(2026, 8, 14, 12, 0)  # Friday
        instants = next_run_instants(
            {"frequency": "weekdays", "time": "07:00"},
            timezone="UTC",
            after=after,
            count=3,
        )
        assert len(instants) == 3
        assert [i.weekday() for i in instants] == [0, 1, 2]  # Mon, Tue, Wed


# ── next_run_instants: weekly / daily ─────────────────────────────────


class TestNextRunWeeklyDaily:
    def test_weekly_specific_day(self) -> None:
        # Wednesday (2) and Friday (4) weekly, 07:00 UTC.
        after = _utc(2026, 8, 14, 8, 0)  # Friday after 07:00
        instants = next_run_instants(
            {"frequency": "weekly", "time": "07:00", "weekdays": [2, 4]},
            timezone="UTC",
            after=after,
            count=2,
        )
        # Next Wednesday 08-19, then Friday 08-21.
        assert [i.day for i in instants] == [19, 21]

    def test_daily_every_day(self) -> None:
        after = _utc(2026, 8, 14, 7, 30)
        instants = next_run_instants(
            {"frequency": "daily", "time": "07:00"},
            timezone="UTC",
            after=after,
            count=3,
        )
        assert [i.day for i in instants] == [15, 16, 17]

    def test_daily_same_day_future_time(self) -> None:
        after = _utc(2026, 8, 14, 6, 0)
        instants = next_run_instants(
            {"frequency": "daily", "time": "07:00"},
            timezone="UTC",
            after=after,
        )
        assert instants[0] == _utc(2026, 8, 14, 7, 0)


# ── DST behaviour ─────────────────────────────────────────────────────


class TestDST:
    def test_spring_forward_nonexistent_time_shifts_forward(self) -> None:
        # Europe/Amsterdam 2026-03-29: 02:00 → 03:00 (spring forward).
        # A schedule at 02:30 local does not exist on that day; the next
        # valid instant is 2026-03-30 02:30 local.
        after = _utc(2026, 3, 28, 12, 0)  # before the transition
        instants = next_run_instants(
            {"frequency": "daily", "time": "02:30"},
            timezone="Europe/Amsterdam",
            after=after,
            count=2,
        )
        # First instant must be 03-29 02:30 Amsterdam → 01:30 UTC (the
        # non-existent 02:30 wall time maps to 01:30 UTC, i.e. 03:30
        # Amsterdam -- actually zoneinfo folds it: the wall time 02:30 on
        # the transition day does not exist, so datetime.combine with
        # tzinfo=Europe/Amsterdam shifts to 03:30 Amsterdam = 01:30 UTC).
        first_utc = instants[0].astimezone(UTC)
        assert first_utc.date().day == 29
        # Local wall clock of the instant must be >= 03:00 (the shifted
        # valid time), never 02:30.
        local = instants[0].astimezone(ZoneInfo("Europe/Amsterdam"))
        assert (local.hour, local.minute) == (3, 30)

    def test_fall_back_never_double_runs(self) -> None:
        # Europe/Amsterdam 2026-10-25: 03:00 → 02:00 (fall back).  A
        # daily 02:30 schedule must produce exactly one instant per local
        # day -- never two for the ambiguous 02:30 wall time.
        after = _utc(2026, 10, 24, 12, 0)
        instants = next_run_instants(
            {"frequency": "daily", "time": "02:30"},
            timezone="Europe/Amsterdam",
            after=after,
            count=3,
        )
        assert len(instants) == 3
        # All three instants fall on distinct local calendar days.
        local_days = [
            i.astimezone(ZoneInfo("Europe/Amsterdam")).date() for i in instants
        ]
        assert len(set(local_days)) == 3

    def test_hourly_24h_anchored_across_dst(self) -> None:
        # "Every 24 hours" across the fall-back (2026-10-25) must never
        # produce two runs on one local calendar day and must stay
        # anchored to the same local wall-clock time (no drift).
        zone = ZoneInfo("Europe/Amsterdam")
        after = _utc(2026, 10, 24, 6, 0)  # 08:00 Amsterdam (CEST)
        instants = next_run_instants(
            {"frequency": "hourly", "interval_hours": 24},
            timezone="Europe/Amsterdam",
            after=after,
            count=4,
        )
        # No two instants on the same local calendar day (the fall-back
        # day 10-25 has 25 hours; an anchored 24h cadence still yields
        # exactly one run that day).
        local_days = [i.astimezone(zone).date() for i in instants]
        assert len(set(local_days)) == len(local_days)
        # Anchored: every instant's local wall-clock time is 08:00
        # (the cadence's anchor), never a drifted value.
        local_times = [
            (i.astimezone(zone).hour, i.astimezone(zone).minute)
            for i in instants
        ]
        assert set(local_times) == {(8, 0)}

    def test_preview_matches_worker_computation(self) -> None:
        """The API preview and the worker use the same pure function --
        identical inputs yield identical instants."""
        from finance_sync.services.sync_schedule import compute_next_run

        after = _utc(2026, 8, 14, 12, 0)
        spec = {
            "frequency": "weekdays",
            "time": "07:00",
            "weekdays": [0, 1, 2, 3, 4],
        }
        direct = next_run_instants(
            spec, timezone="Europe/Amsterdam", after=after, count=3
        )
        # compute_next_run accepts a plain dict shaped like the stored
        # schedule JSON directly (the worker passes the ORM row).
        via_service = compute_next_run(
            spec,
            after=after,
            count=3,
            timezone="Europe/Amsterdam",
        )
        assert via_service == direct


# ── Human-readable summaries ──────────────────────────────────────────


class TestHumanReadable:
    def test_weekdays_summary(self) -> None:
        assert (
            human_readable(
                {"frequency": "weekdays", "time": "07:00"},
                timezone="UTC",
            )
            == "Elke werkdag om 07:00"
        )

    def test_daily_summary(self) -> None:
        assert (
            human_readable(
                {"frequency": "daily", "time": "23:00"}, timezone="UTC"
            )
            == "Elke dag om 23:00"
        )

    def test_weekly_summary(self) -> None:
        assert (
            human_readable(
                {"frequency": "weekly", "time": "07:00", "weekdays": [0, 3]},
                timezone="UTC",
            )
            == "Wekelijks (maandag, donderdag) om 07:00"
        )

    def test_hourly_summary(self) -> None:
        assert (
            human_readable(
                {"frequency": "hourly", "interval_hours": 6},
                timezone="UTC",
            )
            == "Elke 6 uur"
        )

    def test_invalid_returns_ongeldig(self) -> None:
        assert (
            human_readable({"frequency": "bogus"}, timezone="UTC")
            == "Ongeldig schema"
        )


# ── Default schedule ──────────────────────────────────────────────────


class TestDefaultSchedule:
    def test_default_shape(self) -> None:
        spec = default_schedule()
        assert spec == {
            "frequency": "weekdays",
            "time": "07:00",
            "weekdays": [0, 1, 2, 3, 4],
        }
        # Must pass its own validation.
        assert validate_schedule(spec) == spec


# ── Allowlist sanitisation (holdout H5) ──────────────────────────────


class TestSanitiseSchedule:
    def test_keeps_only_known_fields(self) -> None:
        dirty = {
            "frequency": "daily",
            "time": "07:00",
            "weekdays": [0, 1, 2],
            "interval_hours": 12,
            "leaked_secret": "super-secret-token-123",
            "credentials": {"api_key": "sk_live_x"},
        }
        assert sanitise_schedule(dirty) == {
            "frequency": "daily",
            "time": "07:00",
            "weekdays": [0, 1, 2],
            "interval_hours": 12,
        }

    def test_drops_unknown_fields_for_every_frequency_shape(self) -> None:
        for dirty in (
            {"frequency": "daily", "time": "07:00", "stray": 1},
            {"frequency": "hourly", "interval_hours": 4, "stray": "x"},
            {"frequency": "weekly", "weekdays": [1], "stray": ["a"]},
        ):
            cleaned = sanitise_schedule(dirty)
            assert set(cleaned) <= {
                "frequency",
                "time",
                "weekdays",
                "interval_hours",
            }

    def test_non_dict_collapses_to_empty(self) -> None:
        assert sanitise_schedule(None) == {}
        assert sanitise_schedule("garbage") == {}
        assert sanitise_schedule([1, 2]) == {}


from zoneinfo import ZoneInfo
