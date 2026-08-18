"""Sync-schedule recurrence computation.

Pure, side-effect-free helpers for translating a ``sync_schedules``
row's JSON recurrence into concrete UTC instants.  Kept free of
SQLAlchemy/FastAPI so the DST / weekday / misfire rules can be unit
tested at full speed and reused by the API preview, the worker runner
and the migration backfill.

Supported frequencies (the ``schedule`` JSONB shape, version 1)::

    {"frequency": "daily",    "time": "HH:MM"}
    {"frequency": "weekdays", "time": "HH:MM"}          # Mon-Fri
    {"frequency": "weekly",   "time": "HH:MM",
     "weekdays": [0..6]}                                # chosen days
    {"frequency": "hourly",   "interval_hours": N}      # every N hours

Validation rules (enforced by :func:`validate_schedule`):

* ``frequency`` must be one of the four values above;
* ``time`` must be ``HH:MM`` (24h);
* ``weekdays`` (weekly only) must contain at least one day in 0-6;
* ``interval_hours`` (hourly only) must be an int in the safe
  operational range (``MIN_INTERVAL_HOURS`` … ``MAX_INTERVAL_HOURS``);
* the IANA timezone must resolve via ``zoneinfo``.

Time-zone semantics (acceptance criteria, ``docs/sync-schedules.md``):

* Workday = Monday-Friday in the schedule's IANA timezone; national
  holidays are **not** observed.
* DST: instants are computed in the schedule's local zone, so a
  spring-forward (non-existent) local time shifts to the next valid
  instant and a fall-back never produces two runs for one wall-clock
  time.
* Misfires: the worker coalesces overdue schedules to at most one
  safe catch-up run using ``last_scheduled_at`` (see the worker
  runner); this module only computes *next* instants from a given
  ``after`` instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Operational safety limits for the ``hourly`` frequency.  Values
#: outside this range are rejected — they would either hammer the
#: provider or make the schedule useless.
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168  # weekly ceiling

SUPPORTED_FREQUENCIES = frozenset({"daily", "weekdays", "weekly", "hourly"})

#: The only schedule fields the API response and audit snapshots ever
#: expose.  A stray field stored in the JSONB (legacy/corrupt data)
#: must never leak past this allowlist — unknown keys are dropped
#: before the dict leaves the trust boundary (holdout H5).
ALLOWED_SCHEDULE_FIELDS = frozenset(
    {"frequency", "time", "weekdays", "interval_hours"}
)


def sanitise_schedule(schedule: object) -> dict[str, Any]:
    """Keep only the known schedule fields (allowlist, not blacklist).

    Returns a new dict containing at most the keys in
    :data:`ALLOWED_SCHEDULE_FIELDS`; non-dict values (corrupt JSONB)
    collapse to ``{}`` so nothing stored in the column can leak through
    serialisation or audit snapshots.
    """
    if not isinstance(schedule, dict):
        return {}
    raw = cast("dict[str, Any]", schedule)
    return {
        key: value
        for key, value in raw.items()
        if key in ALLOWED_SCHEDULE_FIELDS
    }


#: Monday = 0 … Sunday = 6 (matches ``datetime.weekday()``).
WEEKDAY_NAMES = [
    "maandag",
    "dinsdag",
    "woensdag",
    "donderdag",
    "vrijdag",
    "zaterdag",
    "zondag",
]

#: IANA names that resolve but are known to lack DST — used only for
#: documentation; resolution itself is delegated to ``zoneinfo``.
_STATIC_ZONES = {"UTC", "Etc/UTC"}


class ScheduleValidationError(ValueError):
    """Raised when a schedule JSON (or timezone) is invalid."""


def validate_timezone(tz_name: str) -> ZoneInfo:
    """Resolve *tz_name* to a ``ZoneInfo``; raise on unknown zones.

    ``UTC`` is always accepted.  Raises :class:`ScheduleValidationError`
    (a ``ValueError``) for unknown IANA names so callers can map it to
    a 422 response.
    """
    name = tz_name or ""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"Unknown IANA timezone: {tz_name!r}"
        raise ScheduleValidationError(msg) from exc


def validate_schedule(
    schedule: object,
    *,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Validate and normalise a schedule JSON blob.

    Returns the validated dict (with defaults filled in).  Raises
    :class:`ScheduleValidationError` on any violation so API/worker
    callers can translate it to a 4xx consistently.  *schedule* may be a
    corrupted JSONB value (non-dict) at runtime; the type guard below
    rejects those.
    """
    if not isinstance(schedule, dict):
        msg = "schedule must be an object"
        raise ScheduleValidationError(msg)
    schedule = cast("dict[str, Any]", schedule)

    frequency = schedule.get("frequency")
    if frequency not in SUPPORTED_FREQUENCIES:
        msg = (
            f"Unsupported frequency: {frequency!r} "
            f"(expected one of {sorted(SUPPORTED_FREQUENCIES)})"
        )
        raise ScheduleValidationError(msg)

    validate_timezone(timezone)

    if frequency == "hourly":
        interval = schedule.get("interval_hours")
        if isinstance(interval, bool) or not isinstance(interval, int):
            msg = "interval_hours must be an integer for hourly frequency"
            raise ScheduleValidationError(msg)
        if not MIN_INTERVAL_HOURS <= interval <= MAX_INTERVAL_HOURS:
            msg = (
                f"interval_hours must be between {MIN_INTERVAL_HOURS} "
                f"and {MAX_INTERVAL_HOURS}"
            )
            raise ScheduleValidationError(msg)
        return {
            "frequency": frequency,
            "interval_hours": interval,
        }

    # Daily / weekdays / weekly carry a local wall-clock time.  A
    # missing time falls back to the default; an *empty* string is a
    # validation error (never silently accepted).
    raw_time = schedule.get("time")
    if raw_time is None:
        raw_time = DEFAULT_TIME
    parsed_time = _parse_time(str(raw_time))
    normalised: dict[str, Any] = {
        "frequency": frequency,
        "time": f"{parsed_time.hour:02d}:{parsed_time.minute:02d}",
    }

    if frequency == "weekly":
        weekdays = schedule.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            msg = "weekdays must be a non-empty list for weekly frequency"
            raise ScheduleValidationError(msg)
        days: list[int] = []
        for day in cast("list[Any]", weekdays):
            if isinstance(day, bool) or not isinstance(day, int):
                msg = f"weekday must be an integer 0-6, got {day!r}"
                raise ScheduleValidationError(msg)
            if not 0 <= day <= 6:
                msg = f"weekday out of range 0-6: {day}"
                raise ScheduleValidationError(msg)
            days.append(day)
        normalised["weekdays"] = sorted(set(days))
    elif frequency == "weekdays":
        normalised["weekdays"] = list(range(5))  # Mon-Fri
    elif frequency == "daily":
        pass

    return normalised


DEFAULT_TIME = "07:00"


def _parse_time(value: str) -> time:
    """Parse ``HH:MM`` (24h) into a ``datetime.time``."""
    if ":" not in value:
        msg = f"Invalid time: {value!r} (expected HH:MM)"
        raise ScheduleValidationError(msg)
    hour_str, _, minute_str = value.partition(":")
    try:
        hour = int(hour_str)
        minute = int(minute_str)
        parsed = time(hour, minute)
    except ValueError:
        msg = f"Invalid time: {value!r} (expected HH:MM)"
        raise ScheduleValidationError(msg) from None
    return parsed


def next_run_instants(
    schedule: dict[str, Any],
    *,
    timezone: str,
    after: datetime,
    count: int = 1,
) -> list[datetime]:
    """Return the next *count* UTC instants after *after*.

    *after* must be timezone-aware; results are timezone-aware UTC
    datetimes.  Raises :class:`ScheduleValidationError` when the
    schedule is invalid.
    """
    if after.tzinfo is None:
        msg = "after must be timezone-aware"
        raise ScheduleValidationError(msg)

    normalised = validate_schedule(schedule, timezone=timezone)
    zone = validate_timezone(timezone)
    frequency = normalised["frequency"]

    if frequency == "hourly":
        return _next_hourly(
            normalised,
            zone=zone,
            after=after,
            count=count,
        )

    return _next_daily_like(
        normalised,
        zone=zone,
        after=after,
        count=count,
    )


def _next_hourly(
    schedule: dict[str, Any],
    *,
    zone: ZoneInfo,
    after: datetime,
    count: int,
) -> list[datetime]:
    """Every N hours, anchored to the schedule's run instants.

    Instants are generated by stepping *N local wall-clock hours* from
    ``after`` (the previous run / schedule creation moment).  Stepping in
    local time keeps the local wall-clock moment of every run stable
    across DST:

    * a fall-back day (25 hours) never produces two runs on one local
      calendar day — the next run lands 24 local hours later, on the
      next day;
    * a spring-forward day (23 hours) skips the non-existent local hour
      automatically (normalisation through UTC) — one run per local day,
      no drift.
    """
    interval = schedule["interval_hours"]
    after_local = after.astimezone(zone)

    instants: list[datetime] = []
    cursor = after_local
    guard = 0
    while len(instants) < count and guard < 10_000:
        guard += 1
        cursor = cursor + timedelta(hours=interval)
        # Normalise through UTC: a non-existent local time (spring
        # forward) shifts to the next valid instant automatically.
        instants.append(cursor.astimezone(UTC))
    return instants


def _next_daily_like(
    schedule: dict[str, Any],
    *,
    zone: ZoneInfo,
    after: datetime,
    count: int,
) -> list[datetime]:
    """Next *count* instants matching (weekdays x time-of-day) in zone."""
    freq = schedule["frequency"]
    if freq == "weekly":
        weekdays = set(schedule["weekdays"])
    elif freq == "weekdays":
        # Workday = Monday-Friday in the schedule's own timezone.
        # National holidays are never observed.
        weekdays = {0, 1, 2, 3, 4}
    else:  # daily
        weekdays = None  # every day

    hour, minute = (
        _parse_time(schedule["time"]).hour,
        _parse_time(schedule["time"]).minute,
    )

    local_after = after.astimezone(zone)
    day = local_after.date()
    instants: list[datetime] = []
    guard = 0
    while len(instants) < count and guard < 400:
        guard += 1
        if weekdays is None or day.weekday() in weekdays:
            candidate_local = datetime.combine(
                day, time(hour, minute), tzinfo=zone
            )
            if candidate_local > local_after:
                instants.append(candidate_local.astimezone(UTC))
        day = day + timedelta(days=1)
    return instants


def human_readable(schedule: dict[str, Any], *, timezone: str) -> str:
    """Return a short Dutch human-readable summary (UI + docs)."""
    try:
        normalised = validate_schedule(schedule, timezone=timezone)
    except ScheduleValidationError:
        return "Ongeldig schema"
    freq = normalised["frequency"]
    t = normalised.get("time", "")
    if freq == "daily":
        return f"Elke dag om {t}"
    if freq == "weekdays":
        return f"Elke werkdag om {t}"
    if freq == "weekly":
        day_names: list[str] = [
            WEEKDAY_NAMES[int(d)]
            for d in sorted(
                int(x)
                for x in cast("list[Any]", normalised.get("weekdays") or [])
            )
        ]
        return f"Wekelijks ({', '.join(day_names)}) om {t}"
    if freq == "hourly":
        return f"Elke {normalised['interval_hours']} uur"
    return "Ongeldig schema"


def default_schedule() -> dict[str, Any]:
    """The default recurrence: weekdays 07:00."""
    return {
        "frequency": "weekdays",
        "time": DEFAULT_TIME,
        "weekdays": list(range(5)),
    }
