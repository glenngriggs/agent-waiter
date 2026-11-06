"""Pure schedule math: normalize_schedule, compute_next_run, describe_schedule."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from den.config import load_settings
from den.db import iso, parse_iso
from den.scheduler import ScheduleError, compute_next_run, describe_schedule, normalize_schedule

UTC = timezone.utc
DETROIT = "America/Detroit"
# A fixed "now" so tests do not depend on the wall clock. Detroit is on EST
# (UTC-5) here: DST ended on 2026-11-01.
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path):
    base = load_settings({"DEN_RUNTIME": "local", "DEN_DATA_DIR": str(tmp_path)})
    return dataclasses.replace(base, min_job_interval_minutes=5)


def norm(kind, raw, settings, tz="UTC", now=NOW):
    return normalize_schedule(kind, raw, tz, settings, now)


# --------------------------------------------------------------------------
# once
# --------------------------------------------------------------------------


def test_once_in_the_future_is_stored_as_utc_iso(settings) -> None:
    value = norm("once", "2026-11-06T09:00:00+00:00", settings)
    assert parse_iso(value) == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert value == iso(datetime(2026, 11, 6, 9, 0, tzinfo=UTC))


def test_naive_once_is_read_in_its_timezone(settings) -> None:
    value = norm("once", "2026-11-06T09:00:00", settings, tz=DETROIT)
    # 09:00 EST is 14:00 UTC.
    assert parse_iso(value) == datetime(2026, 11, 6, 14, 0, tzinfo=UTC)
    assert value.endswith("+00:00")


def test_once_with_an_offset_ignores_the_timezone(settings) -> None:
    value = norm("once", "2026-11-06T09:00:00+02:00", settings, tz=DETROIT)
    assert parse_iso(value) == datetime(2026, 11, 6, 7, 0, tzinfo=UTC)


def test_once_in_the_past_is_rejected(settings) -> None:
    with pytest.raises(ScheduleError) as err:
        norm("once", iso(NOW - timedelta(minutes=5)), settings)
    assert str(err.value).strip()  # a sentence the user and the agent can read


def test_once_a_few_seconds_ago_is_still_accepted(settings) -> None:
    value = norm("once", iso(NOW - timedelta(seconds=30)), settings)
    assert parse_iso(value) == NOW - timedelta(seconds=30)


def test_once_more_than_a_year_ahead_is_rejected(settings) -> None:
    with pytest.raises(ScheduleError):
        norm("once", iso(NOW + timedelta(days=367)), settings)


@pytest.mark.parametrize("raw", ["tomorrow at nine", "", "2026-13-01T00:00:00"])
def test_once_that_is_not_iso_is_rejected(settings, raw) -> None:
    with pytest.raises(ScheduleError):
        norm("once", raw, settings)


# --------------------------------------------------------------------------
# interval
# --------------------------------------------------------------------------


def test_interval_at_the_minimum_is_accepted(settings) -> None:
    assert norm("interval", 5, settings) == "5"
    assert norm("interval", 60, settings) == "60"


@pytest.mark.parametrize("raw", [4, 0, -10, 525601])
def test_interval_out_of_range_is_rejected(settings, raw) -> None:
    with pytest.raises(ScheduleError):
        norm("interval", raw, settings)


def test_interval_that_is_not_a_number_is_rejected(settings) -> None:
    with pytest.raises(ScheduleError):
        norm("interval", "often", settings)


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------


def test_cron_every_minute_is_rejected_when_the_minimum_is_five(settings) -> None:
    with pytest.raises(ScheduleError):
        norm("cron", "* * * * *", settings)


@pytest.mark.parametrize("expr", ["*/5 * * * *", "0 9 * * 1-5", "30 7 1 * *"])
def test_valid_cron_is_accepted(settings, expr) -> None:
    assert norm("cron", expr, settings, tz=DETROIT) == expr


@pytest.mark.parametrize("expr", ["61 * * * *", "not cron", "0 0 9 * * 1", ""])
def test_invalid_cron_is_rejected(settings, expr) -> None:
    with pytest.raises(ScheduleError):
        norm("cron", expr, settings)


def test_cron_every_minute_is_fine_when_the_minimum_is_one(settings) -> None:
    relaxed = dataclasses.replace(settings, min_job_interval_minutes=1)
    assert norm("cron", "* * * * *", relaxed) == "* * * * *"


# --------------------------------------------------------------------------
# timezone and kind
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,raw", [("once", "2026-11-06T09:00:00"), ("interval", 60), ("cron", "0 9 * * *")]
)
def test_invalid_timezone_is_rejected(settings, kind, raw) -> None:
    with pytest.raises(ScheduleError):
        norm(kind, raw, settings, tz="Mars/Olympus_Mons")


def test_unknown_kind_is_rejected(settings) -> None:
    with pytest.raises(ScheduleError):
        norm("fortnightly", "x", settings)


def test_schedule_error_is_a_value_error_with_a_status() -> None:
    err = ScheduleError("Too many active jobs.", status=409)
    assert isinstance(err, ValueError)
    assert err.status == 409
    assert ScheduleError("bad").status == 400


# --------------------------------------------------------------------------
# compute_next_run
# --------------------------------------------------------------------------


def _is_utc(dt: datetime) -> bool:
    return dt.utcoffset() == timedelta(0)


def test_next_run_of_a_future_once_is_its_time() -> None:
    at = NOW + timedelta(hours=3)
    nxt = compute_next_run("once", iso(at), "UTC", after=NOW)
    assert nxt == at and _is_utc(nxt)


def test_next_run_of_a_past_once_is_none() -> None:
    assert compute_next_run("once", iso(NOW - timedelta(minutes=2)), "UTC", after=NOW) is None


def test_next_run_of_a_once_within_the_grace_minute_is_kept() -> None:
    at = NOW - timedelta(seconds=30)
    assert compute_next_run("once", iso(at), "UTC", after=NOW) == at


def test_next_run_of_an_interval() -> None:
    nxt = compute_next_run("interval", "45", "UTC", after=NOW)
    assert nxt == NOW + timedelta(minutes=45) and _is_utc(nxt)


def test_cron_next_run_follows_local_time_across_dst_end() -> None:
    # 09:00 in Detroit is 13:00 UTC on Oct 31 (EDT) and 14:00 UTC on Nov 1 (EST).
    after = datetime(2026, 10, 31, 13, 30, tzinfo=UTC)
    nxt = compute_next_run("cron", "0 9 * * *", DETROIT, after=after)
    assert nxt == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    assert _is_utc(nxt)


def test_cron_next_run_follows_local_time_across_dst_start() -> None:
    # DST starts 2027-03-14: 09:00 is 14:00 UTC the day before, 13:00 UTC that day.
    after = datetime(2027, 3, 13, 15, 0, tzinfo=UTC)
    nxt = compute_next_run("cron", "0 9 * * *", DETROIT, after=after)
    assert nxt == datetime(2027, 3, 14, 13, 0, tzinfo=UTC)


def test_cron_next_run_is_strictly_after() -> None:
    at_nine = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)  # 09:00 EST
    nxt = compute_next_run("cron", "0 9 * * *", DETROIT, after=at_nine)
    assert nxt == datetime(2026, 11, 6, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# describe_schedule (the examples in the design doc)
# --------------------------------------------------------------------------


def test_describe_once_in_its_timezone() -> None:
    value = iso(datetime(2026, 11, 6, 14, 0, tzinfo=UTC))
    assert describe_schedule("once", value, DETROIT) == "Once at 2026-11-06 09:00 (America/Detroit)"


def test_describe_interval() -> None:
    assert describe_schedule("interval", "60", "UTC") == "Every 60 minutes"


def test_describe_cron() -> None:
    assert describe_schedule("cron", "0 9 * * 1-5", DETROIT) == "Cron `0 9 * * 1-5` (America/Detroit)"
