"""One cap authority: the caps the live dispatcher publishes on the board bind every pass.

The 17:00:08 breach: the gateway froze ``kanban.*`` at boot and enforced 2/1, while a
CLI dispatch started with ``-p default`` read 5/3 from that home's ``config.yaml`` and
spawned three workers on the same board. The budget gate was fine on both sides — the
two processes simply disagreed about whose numbers were the board's.

The fix: the embedded dispatcher publishes its effective caps to the board
(``board.json`` ``dispatch_caps``) on every tick, and every dispatch entry point —
CLI, dashboard, gateway — resolves the caps through the board first. A home's config
can only seed a board that no dispatcher of record has published caps for.

Red on base ``a44d94a6``: ``Caps: max_in_progress=5 (config)`` and three spawns on a
board whose live dispatcher enforced 2/1.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# The CLI home reads 5/3 — the numbers `-p default` read at 17:00:08.
HOME_CONFIG = (
    "kanban:\n"
    "  max_in_progress: 5\n"
    "  max_in_progress_per_profile: 3\n"
    "  dispatch_in_gateway: true\n"
)

# What the live dispatcher enforces: 2/1.
BOARD_CAPS = (2, 1)


@pytest.fixture
def home(tmp_path, monkeypatch):
    # tests/conftest.py's _hermetic_environment owns HERMES_HOME; write the home
    # config where it points instead of fighting the autouse fixture.
    home = tmp_path / "hermes_test"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(HOME_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Budget arithmetic is the contract here; the runner's memory pressure is not.
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    # Every assignee is claimable from this home (no profile allowlist in play).
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(home):
    with kbc.connect() as c:
        yield c


def _seed(conn, *, running: int, ready: int) -> None:
    """``running`` occupied rows plus ``ready`` claimable ones, each on its own profile."""
    for i in range(running):
        task_id = kb.create_task(conn, title=f"live {i}", assignee=f"live-{i}")
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'probe:4242', "
            "claim_expires = ? WHERE id = ?",
            (int(time.time()) + 3600, task_id),
        )
    for i in range(ready):
        task_id = kb.create_task(conn, title=f"queued {i}", assignee=f"bot-{i}")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    conn.commit()


def _spy_spawn():
    calls: list[str] = []

    def spawn(task, workspace_path, board=None):
        calls.append(getattr(task, "id", str(task)))
        return 424242

    return calls, spawn


def _record_caps_on_board(caps: tuple[int | None, int | None]) -> None:
    """Write the published format straight into ``board.json``.

    Deliberately not through ``publish_board_dispatch_caps``: this is the record a
    live dispatcher leaves behind, and the contract under test is that a tick
    *reads* it. On the base the file is written and ignored — that is the red.
    """
    path = kb.board_dir("default") / "board.json"
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    meta["dispatch_caps"] = {
        "max_in_progress": caps[0],
        "max_in_progress_per_profile": caps[1],
        "published_by": "gateway",
        "published_at": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")


def _board_json_caps() -> dict:
    path = kb.board_dir("default") / "board.json"
    return json.loads(path.read_text(encoding="utf-8")).get("dispatch_caps") or {}


# --- the enforcement seam -------------------------------------------------------


def test_board_caps_bind_the_tick_even_when_the_home_reads_higher(conn):
    """2 running + 3 queued: the board's 2/1 leaves no budget, the home's 5/3 would spawn 3."""
    _seed(conn, running=2, ready=3)
    _record_caps_on_board(BOARD_CAPS)
    calls, spawn = _spy_spawn()
    result = kbd.dispatch_once(
        conn, spawn_fn=spawn,
        max_in_progress=5, max_in_progress_per_profile=3,
        reconcile_orphans=False,
    )
    assert result.spawned == [], f"the board's cap of 2 with 2 running must spawn nothing: {result.spawned}"
    assert calls == [], "spawn_fn must not be reached while the board's cap is exhausted"


def test_the_same_board_without_published_caps_still_spawns_three(conn):
    """Control: the home's 5/3 is what applied before the board published caps.

    This is the red half of the contract: with no dispatcher of record the caller's
    config stands, and that is exactly how three workers were admitted at 17:00:08.
    """
    _seed(conn, running=2, ready=3)
    calls, spawn = _spy_spawn()
    result = kbd.dispatch_once(
        conn, spawn_fn=spawn,
        max_in_progress=5, max_in_progress_per_profile=3,
        reconcile_orphans=False,
    )
    assert len(result.spawned) == 3, f"5/3 - 2 running leaves 3: {result.spawned}"


def test_resolve_dispatch_caps_names_the_board_as_the_source(conn):
    published = kbd.resolve_dispatch_caps(
        "default", max_in_progress=5, max_in_progress_per_profile=3,
    )
    assert published == (5, 3, "config"), "nothing published yet: the caller's numbers stand"

    kbd.publish_board_dispatch_caps(
        "default", max_in_progress=2, max_in_progress_per_profile=1, published_by="probe",
    )
    assert kbd.resolve_dispatch_caps(
        "default", max_in_progress=5, max_in_progress_per_profile=3,
    ) == (2, 1, "board")


def test_the_gateway_tick_publishes_its_caps_to_the_board(home):
    """Behaviour of the embedded dispatcher's tick, not a source reading."""
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher

    settings = _DispatcherSettings(
        interval=60.0, max_spawn=None, max_in_progress=2, failure_limit=3,
        stale_timeout_seconds=0, reconcile_orphans=False, default_assignee=None,
        max_in_progress_per_profile=1,
    )
    _KanbanDispatcher(kb, settings).tick_once_for_board("default")

    assert _board_json_caps() == {
        "max_in_progress": 2,
        "max_in_progress_per_profile": 1,
        "published_by": "gateway",
        "published_at": _board_json_caps()["published_at"],
    }


# --- the operator-visible line --------------------------------------------------


def test_cli_prints_the_board_as_the_cap_source_and_spawns_nothing(conn, capsys):
    _seed(conn, running=2, ready=3)
    _record_caps_on_board(BOARD_CAPS)
    from hermes_cli import kanban_ops

    rc = kanban_ops._cmd_dispatch(argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=False, board=None, force=True,
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Caps: max_in_progress=2 (board), max_in_progress_per_profile=1, max_spawn=unset" in out
    assert "Spawned:      0" in out
