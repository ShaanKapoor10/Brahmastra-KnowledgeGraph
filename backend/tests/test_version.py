"""
Which code is this process running -- and is it behind the files beside it?

Both failures this exists for happened on one day. The MCP server ran the old
resolver and reported 837 clusters where fresh code said 852; the Docker stack
ran a three-day-old image whose scheduler reverted every fix. Each produced
numbers that looked exactly as authoritative as the right ones.
"""
from __future__ import annotations

from brahmastra import version


def test_a_fresh_process_is_current():
    s = version.status()
    assert s["fingerprint"] == s["on_disk"]
    assert s["stale"] is False
    assert "warning" not in s


def test_a_process_behind_its_files_says_so(monkeypatch):
    """The MCP-server case: loaded at startup, files changed since."""
    monkeypatch.setattr(version, "LOADED", "000000000000")
    s = version.status()
    assert s["stale"] is True
    assert "restart" in s["warning"]


def test_the_fingerprint_changes_when_the_code_does(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    before = version.fingerprint_of_disk()
    (tmp_path / "a.py").write_text("x = 2\n")
    assert version.fingerprint_of_disk() != before


def test_line_endings_do_not_count_as_a_change(tmp_path, monkeypatch):
    """
    A Windows checkout and the Linux container built from it must agree on
    what "the same code" means, or every comparison between them would report
    a difference that is not one.
    """
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    crlf = version.fingerprint_of_disk()
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
    assert version.fingerprint_of_disk() == crlf


def test_a_renamed_file_is_a_change(tmp_path, monkeypatch):
    """Same bytes under another name is different code: imports resolve by name."""
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    before = version.fingerprint_of_disk()
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    assert version.fingerprint_of_disk() != before


def test_health_reports_the_code_without_touching_a_database():
    """Liveness must stay dependency-free; the fingerprint is a file hash."""
    import asyncio

    import main

    body = asyncio.run(main.health())
    assert body["status"] == "ok"
    assert body["code"]["fingerprint"] == version.LOADED


def test_a_server_that_predates_the_fingerprint_is_behind(monkeypatch, capsys):
    """No `code` in /health means older than this module -- behind by definition."""
    monkeypatch.setattr(version, "compare",
                        lambda url: {"here": "abc", "there": None, "behind": True})
    assert version.main(["--against", "http://example"]) == 1
    assert "predates" in capsys.readouterr().out


def test_a_matching_server_is_current(monkeypatch, capsys):
    monkeypatch.setattr(version, "compare",
                        lambda url: {"here": "abc", "there": "abc", "behind": False})
    assert version.main(["--against", "http://example"]) == 0
    assert "current" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The commit is read from .git, never by running git
# ---------------------------------------------------------------------------
#
# Running `git` froze the stdio MCP server for 30 minutes at a time: the child
# inherited the server's stdin pipe, blocked on it at startup on Windows, and
# subprocess's timeout killed only the wrapper, then waited without limit for
# the inner git's pipes. version.status() runs at the start of every pipeline
# run, so every brahmastra_run_pipeline call could stall the whole server.

def _checkout(tmp_path, head: str, refs: dict[str, str] | None = None,
              packed: str | None = None):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n")
    for name, sha in (refs or {}).items():
        (git / name).parent.mkdir(parents=True, exist_ok=True)
        (git / name).write_text(sha + "\n")
    if packed is not None:
        (git / "packed-refs").write_text(packed)
    pkg = tmp_path / "backend" / "brahmastra"
    pkg.mkdir(parents=True)
    return pkg


def test_the_commit_comes_from_a_loose_branch_ref(tmp_path, monkeypatch):
    pkg = _checkout(tmp_path, "ref: refs/heads/main",
                    refs={"refs/heads/main": "4e8ee22aa1b2c3d4e5f6"})
    monkeypatch.setattr(version, "PACKAGE", pkg)
    monkeypatch.delenv("BRAHMASTRA_GIT_COMMIT", raising=False)
    assert version._git_commit() == "4e8ee22"


def test_the_commit_comes_from_packed_refs(tmp_path, monkeypatch):
    pkg = _checkout(tmp_path, "ref: refs/heads/feat/x",
                    packed="# pack-refs with: peeled\n"
                           "1111111aaaa refs/heads/main\n"
                           "2222222bbbb refs/heads/feat/x\n^3333333cccc\n")
    monkeypatch.setattr(version, "PACKAGE", pkg)
    monkeypatch.delenv("BRAHMASTRA_GIT_COMMIT", raising=False)
    assert version._git_commit() == "2222222"


def test_a_detached_head_is_its_own_commit(tmp_path, monkeypatch):
    pkg = _checkout(tmp_path, "abcdef0123456789")
    monkeypatch.setattr(version, "PACKAGE", pkg)
    monkeypatch.delenv("BRAHMASTRA_GIT_COMMIT", raising=False)
    assert version._git_commit() == "abcdef0"


def test_a_worktree_follows_its_gitdir_file(tmp_path, monkeypatch):
    main = tmp_path / "main"
    _checkout(main, "ref: refs/heads/main", refs={"refs/heads/main": "9999999zzzz"})
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/main\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    (wt / "backend" / "brahmastra").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    monkeypatch.setattr(version, "PACKAGE", wt / "backend" / "brahmastra")
    monkeypatch.delenv("BRAHMASTRA_GIT_COMMIT", raising=False)
    assert version._git_commit() == "9999999"


def test_no_checkout_means_no_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    monkeypatch.delenv("BRAHMASTRA_GIT_COMMIT", raising=False)
    monkeypatch.setattr(version, "_git_dir", lambda: (_ for _ in ()).throw(ValueError("none")))
    assert version._git_commit() is None


def test_status_never_starts_a_process(monkeypatch):
    """The regression itself: nothing on this path may spawn `git` again."""
    import subprocess

    def refuse(*a, **k):
        raise AssertionError("version.status() started a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    version.status()
