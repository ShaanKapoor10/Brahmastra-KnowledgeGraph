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

TOKENS (phase C). Every AI call reports what the provider billed
(llm._record_tokens), and record_tokens() books it to whoever the work is for:

    a question            the person asking         (the request's identity)
    reading a note        the note's author         (note_authors, set on write)
    processing a meeting  the person who uploaded it
    graph, summaries,     the workspace: that work serves everyone in it
      entity checks

`charging()` sets that explicitly where the request's identity is not the
right answer (a background job has none; a pipeline run started by one
person builds a graph for everyone). Each booking goes to a daily ledger (for
the admin Usage tab) and to day/month token counters for the person or
workspace and for the company, which the budget check reads BEFORE each call.

A spent budget raises llm.BudgetExhausted, a kind of LLMQuotaExhausted, so
every caller's existing handling for a spent quota applies unchanged: notes
stay pending and are retried, nothing is lost. Unlike a provider's quota it
never falls back to another provider -- the budget is the company's, not the
provider's.
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
import time
from contextvars import ContextVar
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
    );
    CREATE TABLE IF NOT EXISTS usage_ledger (
        day       TEXT   NOT NULL,
        who       TEXT   NOT NULL,
        purpose   TEXT   NOT NULL,
        provider  TEXT   NOT NULL,
        model     TEXT   NOT NULL,
        input     BIGINT NOT NULL DEFAULT 0,
        output    BIGINT NOT NULL DEFAULT 0,
        thoughts  BIGINT NOT NULL DEFAULT 0,
        calls     BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (day, who, purpose, provider, model)
    );
    CREATE TABLE IF NOT EXISTS note_authors (
        workspace_id TEXT NOT NULL,
        note_id      TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        created_at   DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (workspace_id, note_id)
    );
    CREATE TABLE IF NOT EXISTS usage_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
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

    def book(self, day: str, who: str, purpose: str, provider: str, model: str,
             tokens_in: int, tokens_out: int, thoughts: int, calls: int = 1) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO usage_ledger (day, who, purpose, provider, model, input, output, thoughts, calls) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (day, who, purpose, provider, model) "
                "DO UPDATE SET input = usage_ledger.input + ?, output = usage_ledger.output + ?, "
                "thoughts = usage_ledger.thoughts + ?, calls = usage_ledger.calls + ?"),
                (day, who, purpose, provider, model, tokens_in, tokens_out, thoughts, calls,
                 tokens_in, tokens_out, thoughts, calls))

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(sql), params)
            return [self._dict(r) for r in cur.fetchall()]

    def run(self, sql: str, params: tuple = ()) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(sql), params)


_counters: _Counters | None = None


def _store() -> _Counters:
    global _counters
    if _counters is None:
        _counters = _Counters(workspace="default")
    return _counters


def _limits_for(identity: Any) -> dict[str, Any]:
    from brahmastra.accounts import store

    return store().effective_limits(identity.user_id, identity.role)


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

    limits = store().effective_limits(user_id, role)
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
    for period in ("day", "month"):
        rows.append({"meter": "tokens", "period": period, "limit_key": f"tokens_{period}",
                     "used": totals.get(("tokens", now[period][0]), 0),
                     "limit": limits.get(f"tokens_{period}"), "resets_at": now[period][1]})
    return rows


# ================================================================ tokens (phase C)

COMPANY = "company"
_charge: ContextVar[tuple[str | None, str | None] | None] = ContextVar("brahmastra_charge", default=None)
_purpose: ContextVar[str] = ContextVar("brahmastra_charge_purpose", default="other")

# $ per million tokens (input, output). Thinking is billed as output. Used only
# to ESTIMATE cost on the Usage tab; the token counts themselves are billed
# figures. A model not listed shows tokens without a price.
PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "deepseek-v4.1-flash": (0.30, 1.20),
}


def workspace_key(workspace: str) -> str:
    return f"ws:{workspace}"


@contextlib.contextmanager
def charging(*, user_id: str | None = None, workspace: str | None = None, purpose: str | None = None):
    """Book AI calls made inside this block to a person or a workspace.

    Give a user_id, or a workspace, or neither (keep who it already is and set
    only the purpose).
    """
    tokens = []
    if user_id or workspace:
        tokens.append((_charge, _charge.set((user_id, workspace))))
    if purpose:
        tokens.append((_purpose, _purpose.set(purpose)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def current_who() -> str:
    """Who the AI call being made now is booked to: a user id or 'ws:<id>'."""
    explicit = _charge.get()
    if explicit is not None:
        user_id, workspace = explicit
        if user_id:
            return user_id
        if workspace:
            return workspace_key(workspace)
    from brahmastra.accounts import current_identity
    from brahmastra.workspace import current_workspace

    me = current_identity()
    if me is not None and me.user_id:
        return me.user_id
    return workspace_key(current_workspace())


def record_tokens(provider: str, model: str, tokens_in: int, tokens_out: int, thoughts: int) -> None:
    """Book one call's billed tokens. Never raises: a meter must not fail a call."""
    try:
        total = int(tokens_in) + int(tokens_out) + int(thoughts)
        who, now = current_who(), periods()
        store = _store()
        store.book(now["day"][0][2:], who, _purpose.get(), provider, model or "", int(tokens_in),
                   int(tokens_out), int(thoughts))
        for owner in (who, COMPANY):
            for period in ("day", "month"):
                store.add(owner, "tokens", now[period][0], total)
    except Exception:                                         # noqa: BLE001
        pass


def record_cache_hit() -> None:
    """A reply served from the cache: a call that cost nothing. Never raises."""
    try:
        store = _store()
        store.book(periods()["day"][0][2:], current_who(), _purpose.get(), "cache", "", 0, 0, 0)
    except Exception:                                         # noqa: BLE001
        pass


# -- budgets

def company_budget() -> dict[str, int | None]:
    """The company's daily and monthly token budgets; None means no budget.

    An admin sets them on the Usage tab; TOKEN_BUDGET_DAY / TOKEN_BUDGET_MONTH
    in the server's settings are a ceiling the admin cannot exceed.
    """
    stored = {r["key"]: r["value"] for r in _store().rows("SELECT key, value FROM usage_settings")}
    out: dict[str, int | None] = {}
    for period in ("day", "month"):
        env = os.environ.get(f"TOKEN_BUDGET_{period.upper()}", "").strip()
        ceiling = int(env) if env.isdigit() else None
        chosen = stored.get(f"tokens_{period}")
        value = int(chosen) if chosen and chosen.isdigit() else ceiling
        if ceiling is not None and value is not None:
            value = min(value, ceiling)
        out[period] = value
    return out


def set_company_budget(day: int | None, month: int | None) -> dict[str, int | None]:
    for period, value in (("day", day), ("month", month)):
        key = f"tokens_{period}"
        _store().run("DELETE FROM usage_settings WHERE key = ?", (key,))
        if value is not None:
            if value < 0:
                raise ValueError("A budget must be 0 or more.")
            _store().run("INSERT INTO usage_settings (key, value) VALUES (?, ?)", (key, str(int(value))))
    return company_budget()


_plan_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_plan_cache_lock = threading.Lock()


def _person_limits(user_id: str) -> dict[str, Any] | None:
    """A person's plan limits, cached for 30 s: the budget check runs before
    every AI call, and a plan change applying half a minute later is fine."""
    now = time.monotonic()
    with _plan_cache_lock:
        hit = _plan_cache.get(user_id)
        if hit and now - hit[0] < 30:
            return hit[1]
    from brahmastra.accounts import store

    user = store().get_user(user_id)
    limits = store().effective_limits(user_id, user["role"]) if user else None
    with _plan_cache_lock:
        _plan_cache[user_id] = (now, limits or {})
    return limits


def budget_problem(who: str | None = None) -> str | None:
    """Why an AI call may not start now, or None. Checked BEFORE every call.

    The company budget applies to everyone. A person's plan budget applies to
    work booked to that person; shared workspace work meets only the company's.
    """
    who = who or current_who()
    now = periods()
    checks: list[tuple[str, str, int | None, str]] = []
    company = company_budget()
    checks += [(COMPANY, "day", company["day"], "The company's AI budget for today"),
               (COMPANY, "month", company["month"], "The company's AI budget for this month")]
    if not who.startswith("ws:"):
        limits = _person_limits(who) or {}
        checks += [(who, "day", limits.get("tokens_day"), "Your AI usage for today"),
                   (who, "month", limits.get("tokens_month"), "Your AI usage for this month")]
    for owner, period, limit, what in checks:
        if limit is None:
            continue
        used = _store().totals(owner, [now[period][0]]).get(("tokens", now[period][0]), 0)
        if used >= limit:
            when = ("at midnight" if period == "day" else
                    f"on {datetime.fromtimestamp(now['month'][1], _tz()).day} "
                    f"{datetime.fromtimestamp(now['month'][1], _tz()):%B}")
            return f"{what} is used up. It resets {when}; search and browsing still work."
    return None


# -- note authors

def remember_author(note_id: str) -> None:
    """Record who added a note, the first time it is written. Never raises."""
    try:
        from brahmastra.accounts import current_identity
        from brahmastra.workspace import current_workspace

        me = current_identity()
        if me is None or not me.user_id:
            return
        _store().run("INSERT INTO note_authors (workspace_id, note_id, user_id, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (workspace_id, note_id) DO NOTHING",
                     (current_workspace(), note_id, me.user_id, time.time()))
    except Exception:                                         # noqa: BLE001
        pass


def author_of(note_id: str) -> str | None:
    from brahmastra.workspace import current_workspace

    try:
        rows = _store().rows("SELECT user_id FROM note_authors WHERE workspace_id = ? AND note_id = ?",
                             (current_workspace(), note_id))
    except Exception:                                         # noqa: BLE001
        return None
    return rows[0]["user_id"] if rows else None


# -- reading it back, for the Usage tab

def estimated_cost(model: str, tokens_in: int, tokens_out: int, thoughts: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (tokens_in * price[0] + (tokens_out + thoughts) * price[1]) / 1_000_000


def _sum(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = {"input": 0, "output": 0, "thoughts": 0, "calls": 0, "cost": 0.0, "priced": True}
    for r in rows:
        if r["provider"] == "cache":
            continue
        for k in ("input", "output", "thoughts", "calls"):
            total[k] += int(r[k])
        cost = estimated_cost(r["model"], int(r["input"]), int(r["output"]), int(r["thoughts"]))
        if cost is None:
            total["priced"] = False
        else:
            total["cost"] += cost
    total["tokens"] = total["input"] + total["output"] + total["thoughts"]
    total["cost"] = round(total["cost"], 4)
    return total


def overview(days: int = 30) -> dict[str, Any]:
    """Everything the admin Usage tab shows. Counts only: no text of any kind."""
    from brahmastra.accounts import store as accounts

    days = max(1, min(int(days), 366))
    now = periods()
    today, month = now["day"][0][2:], now["month"][0][2:]
    first = (_tz_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = _store().rows("SELECT * FROM usage_ledger WHERE day >= ?", (min(first, month + "-01"),))
    in_month = [r for r in rows if r["day"].startswith(month)]

    daily: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["day"] >= first and r["provider"] != "cache":
            d = daily.setdefault(r["day"], {"input": 0, "output": 0, "thoughts": 0})
            for k in d:
                d[k] += int(r[k])
    series = []
    for i in range(days):
        day = (_tz_now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        series.append({"day": day, **daily.get(day, {"input": 0, "output": 0, "thoughts": 0})})

    def grouped(key) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in in_month:
            groups.setdefault(key(r), []).append(r)
        return sorted(({"key": k, **_sum(v)} for k, v in groups.items()), key=lambda g: -g["tokens"])

    company = company_budget()
    people = []
    for u in accounts().list_users():
        mine = [r for r in in_month if r["who"] == u["id"]]
        limits = accounts().effective_limits(u["id"], u["role"])
        units = {m: v for (m, p), v in _store().totals(u["id"], [now["month"][0]]).items()}
        today_tokens = sum(int(r["input"]) + int(r["output"]) + int(r["thoughts"])
                           for r in rows if r["who"] == u["id"] and r["day"] == today)
        people.append({"id": u["id"], "email": u["email"], "name": u["name"], "plan": u["plan"],
                       "role": u["role"], "disabled": u["disabled"],
                       "questions": units.get("questions", 0), "notes": units.get("notes", 0),
                       "words": units.get("words", 0),
                       "meeting_hours": round(units.get("meeting_minutes", 0) / 60, 1),
                       "tokens_today": today_tokens, "tokens_day_limit": limits.get("tokens_day"),
                       "tokens_month_limit": limits.get("tokens_month"), **_sum(mine)})
    people.sort(key=lambda p: -p["tokens"])

    company_today = _sum([r for r in rows if r["day"] == today])
    return {
        "month": month, "today": today, "days": days,
        "company": {"today": company_today, "month": _sum(in_month), "budget": company,
                    "cached_replies_month": sum(int(r["calls"]) for r in in_month if r["provider"] == "cache")},
        "series": series,
        "by_purpose": grouped(lambda r: r["purpose"]),
        "by_model": grouped(lambda r: f'{r["provider"]}:{r["model"]}' if r["provider"] != "cache" else "cache"),
        "workspaces": [g for g in grouped(lambda r: r["who"]) if g["key"].startswith("ws:")],
        "people": people,
    }


def person(user_id: str, days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(days), 366))
    first = (_tz_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = _store().rows("SELECT * FROM usage_ledger WHERE who = ? AND day >= ?", (user_id, first))
    by_day: dict[str, list[dict[str, Any]]] = {}
    by_purpose: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
        by_purpose.setdefault(r["purpose"], []).append(r)
    return {"user_id": user_id, "days": days,
            "daily": [{"day": d, **_sum(v)} for d, v in sorted(by_day.items())],
            "by_purpose": sorted(({"key": k, **_sum(v)} for k, v in by_purpose.items()),
                                 key=lambda g: -g["tokens"])}


def reset() -> None:
    """Forget every count (tests only)."""
    global _counters
    try:
        s = _store()
        s.init_schema()
        with s._cursor() as cur:
            for table in ("usage_counters", "usage_ledger", "note_authors", "usage_settings"):
                cur.execute(f"DELETE FROM {table}")
    except Exception:                                         # noqa: BLE001
        pass
    _counters = None
    _plan_cache.clear()
