from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Task:
    title: str
    due: Optional[str] = None      # ISO 8601, e.g. "2026-10-23T09:00:00+00:00"
    done: bool = False


def add(tasks: list[Task], title: str, due: Optional[str] = None) -> Task:
    task = Task(title=title, due=due)
    tasks.append(task)
    return task


def complete(tasks: list[Task], title: str) -> None:
    for task in tasks:
        if task.title == title:
            task.done = True


def due_today(tasks: list[Task], now: Optional[datetime] = None) -> list[Task]:
    now = now or datetime.now(timezone.utc)
    return [t for t in tasks if t.due and datetime.fromisoformat(t.due).date() == now.date()]


def export(tasks: list[Task]) -> list[dict]:
    return [{"title": t.title, "due": t.due, "done": t.done} for t in tasks]
