from datetime import datetime

from larkspur import add, complete, due_today, export


def test_add_and_complete():
    tasks = []
    add(tasks, "write report")
    complete(tasks, "write report")
    assert tasks[0].done


def test_due_today():
    tasks = []
    add(tasks, "call the bank", due=datetime.now().isoformat())
    assert [t.title for t in due_today(tasks)] == ["call the bank"]


def test_export_keeps_the_mobile_format():
    tasks = []
    add(tasks, "pay rent", due="2026-11-01T09:00:00+00:00")
    assert export(tasks) == [{"title": "pay rent", "due": "2026-11-01T09:00:00+00:00", "done": False}]
