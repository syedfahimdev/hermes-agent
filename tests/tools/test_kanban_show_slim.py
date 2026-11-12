"""kanban_show ``view`` arg: slim (default) must not repeat what ``worker_context`` carries."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


BODY = "implement the frobnicator with care"
SHORT_COMMENT = "short operator note"
LONG_COMMENT = "long operator note " + "x" * 3000


@pytest.fixture
def busy_task(worker_env):
    """The worker's task with a body, two comments and a mix of noisy/meaningful events."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (BODY, worker_env))
            for i in range(30):
                kb._append_event(conn, worker_env, "heartbeat", {"n": i})
            kb._append_event(conn, worker_env, "edited", {"what": "title"})
        kb.add_comment(conn, worker_env, "alice", SHORT_COMMENT)
        kb.add_comment(conn, worker_env, "bob", LONG_COMMENT)
    finally:
        conn.close()
    return worker_env


def _show(**args) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_show(args))


def test_full_view_is_the_old_payload(busy_task):
    full = _show(view="full")
    assert set(full) == {
        "task", "parents", "unsatisfied_parents", "children", "comments", "events", "runs",
        "worker_context"}
    assert full["task"]["body"] == BODY
    assert [c["body"] for c in full["comments"]] == [SHORT_COMMENT, LONG_COMMENT]
    slim = _show()
    assert slim["view"] == "slim"
    for key in ("parents", "unsatisfied_parents", "children", "worker_context"):
        assert slim[key] == full[key]
    assert slim["task"] == {**full["task"], "body": slim["task"]["body"]}


def test_slim_replaces_body_with_pointer(busy_task):
    slim = _show()
    assert slim["task"]["body"] == "(in worker_context: ## Body)"
    assert "## Body" in slim["worker_context"] and BODY in slim["worker_context"]


def test_slim_drops_comments_already_in_worker_context(busy_task):
    slim = _show()
    assert [c["body"] for c in slim["comments"]] == [LONG_COMMENT]
    assert slim["comments_in_worker_context"] == 1
    assert SHORT_COMMENT in slim["worker_context"]


def test_slim_drops_noisy_events(busy_task):
    slim = _show()
    assert all(e["kind"] != "heartbeat" for e in slim["events"])
    assert slim["events_omitted"] >= 30
    assert "edited" in {e["kind"] for e in slim["events"]}


def test_slim_is_smaller_than_full(busy_task):
    from tools import kanban_tools as kt
    assert len(kt._handle_show({})) < len(kt._handle_show({"view": "full"}))


@pytest.mark.parametrize("bad", ["tiny", "FULL", 3])
def test_bad_view_is_an_error(busy_task, bad):
    out = _show(view=bad)
    assert "view must be 'slim' or 'full'" in out["error"]


def _run(**over) -> dict:
    return {"id": 1, "summary": None, "error": None, "metadata": None, "ended_at": 5, **over}


def test_slim_run_recent_closed_fits_cap_is_dropped_else_kept():
    from tools.kanban_tools import _slim_run
    fits = _slim_run(_run(summary="done", metadata={"a": 1}), in_context=True)
    assert (fits["summary"], fits["error"], fits["metadata"], fits["in_worker_context"]) == (
        None, None, None, True)
    big = _run(summary="x" * 5000)
    assert _slim_run(big, in_context=True) == big


def test_slim_run_old_closed_is_truncated():
    from tools.kanban_tools import _slim_run
    out = _slim_run(_run(summary="x" * 400, metadata={"k": "y" * 400}), in_context=False)
    assert out["summary"].startswith("x" * 300 + "… [slim:")
    assert out["metadata"].startswith("… [slim:") and out["error"] is None
