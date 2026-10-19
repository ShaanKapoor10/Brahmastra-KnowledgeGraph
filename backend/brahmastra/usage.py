"""
Usage against a plan's limits: questions, notes, words and meeting minutes,
per person, per day and per calendar month.

Counted where the work ENTERS the system -- a question asked, a note added, a
meeting submitted -- and charged to the person whose request it is, so a
person is stopped before the cost exists rather than billed after it.

ATOMIC, ACROSS WORKERS. Each charge is one upsert that adds and returns the new
total; if that total is over the limit, the same amount is taken back and the
request refused. Two requests for the last slot therefore cannot both get it,
on any number of workers -- the same guarantee ratelimit.py gives its shared
buckets. The rate-limit table could not be reused: it forgets windows older
than a day, and a month must be remembered for a month.

Periods follow the install's time zone (TZ): a daily limit resets at midnight
where the people are, a monthly one on the 1st. The service key and anonymous
development mode have no user and are never counted.

Phase B of plans counts units. Tokens (phase C) are counted where they are
billed, in llm.py, not here.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brahmastra.sidecar import SidecarStore

# meter -> (monthly limit key, daily limit key or None, how many of the meter
# one unit of the limit is). Meeting limits are in HOURS, counted in minutes.
METERS: dict[str, tuple[str, str | None, int]] = {
    "questions": ("questions_month", "questions_day", 1),
    "notes": ("notes_month", "notes_day", 1),
    "words": ("words_month", None, 1),
    "meeting_minutes": ("meeting_hours_month", None, 60),
}

# How a person reads each meter in a message.
_NOUN = {"questions": "questions", "notes": "notes", "words": "words of storage",
         "meeting_minutes": "meeting minutes"}

# A meeting's length, estimated from its words when it carries no timestamps:
# conversational speech runs at about 150 words a minute.
WORDS_PER_MINUTE = 150


@dataclass
class LimitReached(Exception):
    meter: str
    period: str            # "day" | "month"
    limit: int
    reset_at: float

    def __str__(self) -> str:
        noun = _NOUN.get(self.meter, self.meter)
        shown = self.limit
        if self.meter == "meeting_minutes":
            noun, shown = "meeting hours", self.limit // 60
        if self.period == "month":
            reset = datetime.fromtimestamp(self.reset_at, _tz())
            return (f"You've used your {shown:,} {noun} this month. They reset on "
                    f"{reset.day} {reset:%B}. Ask an admin for more.")
        return f"You've used your {shown:,} {noun} today. They reset at midnight. Ask an admin for more."

    def body(self) -> dict[str, Any]:
        return {"detail": str(self), "code": "plan_limit", "meter": self.meter, "period": self.period,
                "limit": self.limit, "reset_at": self.reset_at}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("TZ", "").strip() or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _tz_now() -> datetime:
    return datetime.fromtimestamp(time.time(), _tz())


def periods(now: datetime | None = None) -> dict[str, tuple[str, float]]:
    """{"day": (key, resets_at), "month": (key, resets_at)} for `now`."""
    now = now or _tz_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    return {"day": (f"d:{day_start:%Y-%m-%d}", next_day.timestamp()),
            "month": (f"m:{month_start:%Y-%m}", next_month.timestamp())}


class _Counters(SidecarStore):
    """Usage per person, meter and period. Global, not workspace data."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS usage_counters (
        who     TEXT   NOT NULL,
        meter   TEXT   NOT NULL,
        period  TEXT   NOT NULL,
        amount  BIGINT NOT NULL,
        PRIMARY KEY (who, meter, period)
    )
    """

    def add(self, who: str, meter: str, period: str, amount: int) -> int:
        """Add `amount` (may be negative) and return the new total, atomically."""
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO usage_counters (who, meter, period, amount) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (who, meter, period) DO UPDATE SET amount = usage_counters.amount + ? "
                "RETURNING amount"), (who, meter, period, amount, amount))
            return int(self._dict(cur.fetchone())["amount"])

    def totals(self, who: str, period_keys: list[str]) -> dict[tuple[str, str], int]:
        self.init_schema()
        marks = ", ".join("?" for _ in period_keys)
        with self._cursor() as cur:
            cur.execute(self._ph(f"SELECT meter, period, amount FROM usage_counters "
                                 f"WHERE who = ? AND period IN ({marks})"), (who, *period_keys))
            return {(r["meter"], r["period"]): int(r["amount"])
                    for r in (self._dict(x) for x in cur.fetchall())}


_counters: _Counters | None = None


def _store() -> _Counters:
    global _counters
    if _counters is None:
        _counters = _Counters(workspace="default")
    return _counters


def _limits_for(identity: Any) -> dict[str, Any]:
    from brahmastra.accounts import store

    return store().plan_for(identity.user_id, identity.role)["limits"]


def charge(identity: Any, meter: str, amount: int = 1) -> None:
    """Count `amount` of `meter` against this person's day and month.

    Raises LimitReached, having counted nothing, when either would go over.
    """
    if identity is None or not getattr(identity, "user_id", None) or amount <= 0:
        return
    month_key, day_key, scale = METERS[meter]
    limits = _limits_for(identity)
    store, done = _store(), []
    try:
        for period, limit_key in (("month", month_key), ("day", day_key)):
            if limit_key is None:
                continue
            key, resets_at = periods()[period]
            total = store.add(identity.user_id, meter, key, amount)
            done.append(key)
            limit = limits.get(limit_key)
            if limit is not None and total > limit * scale:
                raise LimitReached(meter, period, limit * scale, resets_at)
    except LimitReached:
        for key in done:
            store.add(identity.user_id, meter, key, -amount)
        raise


def refund(identity: Any, meter: str, amount: int = 1) -> None:
    """Take a charge back: the request it paid for failed."""
    if identity is None or not getattr(identity, "user_id", None) or amount <= 0:
        return
    month_key, day_key, _ = METERS[meter]
    for period, limit_key in (("month", month_key), ("day", day_key)):
        if limit_key is not None:
            _store().add(identity.user_id, meter, periods()[period][0], -amount)


def words_in(text: str) -> int:
    return len((text or "").split())


def meeting_minutes(text: str) -> int:
    """A meeting's length in minutes, from its words; at least one."""
    return max(1, math.ceil(words_in(text) / WORDS_PER_MINUTE))


def charge_note(identity: Any, content: str) -> None:
    """One note and its words; neither is counted if either is over."""
    charge(identity, "notes", 1)
    try:
        charge(identity, "words", words_in(content))
    except LimitReached:
        refund(identity, "notes", 1)
        raise


def report(user_id: str, role: str) -> list[dict[str, Any]]:
    """Each limited meter's use this day and month, for the usage panel."""
    from brahmastra.accounts import store

    limits = store().plan_for(user_id, role)["limits"]
    now = periods()
    totals = _store().totals(user_id, [now["day"][0], now["month"][0]])
    rows = []
    for meter, (month_key, day_key, scale) in METERS.items():
        for period, limit_key in (("month", month_key), ("day", day_key)):
            if limit_key is None:
                continue
            limit = limits.get(limit_key)
            used = totals.get((meter, now[period][0]), 0)
            rows.append({"meter": meter, "period": period, "limit_key": limit_key,
                         "used": used / scale if scale > 1 else used,
                         "limit": limit, "resets_at": now[period][1]})
    return rows


def reset() -> None:
    """Forget every count (tests only)."""
    global _counters
    try:
        s = _store()
        s.init_schema()
        with s._cursor() as cur:
            cur.execute("DELETE FROM usage_counters")
    except Exception:                                         # noqa: BLE001
        pass
    _counters = None
