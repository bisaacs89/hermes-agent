"""A CLI dispatch is refused while the gateway's embedded dispatcher is live.

``_check_dispatcher_presence`` has answered "is a gateway with
``kanban.dispatch_in_gateway`` alive for this home" since #71211, but only the
dashboard and ``create`` ever consulted it, as a warning. ``hermes kanban
dispatch`` — the path a cron tick takes — never asked.

That is the actual 17:00:08 defect, and it is not a budget question: the CLI
tick and the gateway claimed from the same board in the same second, and the
three workers the CLI spawned died with the CLI's pipe (``worker_lost``
EOFError, claimer ``briPC:53736``). Budgets cannot separate two drivers that
are both inside their own numbers; only one driver can be allowed.

``--force`` stays for the deliberate one-off; a probe that cannot answer
(``_check_dispatcher_presence`` fails OPEN with ``(True, "")``) must not brick
every manual dispatch.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

HOME_CONFIG = (
    "kanban:\n"
    "  max_in_progress: 2\n"
    "  max_in_progress_per_profile: 1\n"
    "  dispatch_in_gateway: true\n"
)

LIVE = (True, "gateway pid=4904, dispatch enabled")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_test"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(HOME_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _args(**over):
    base = dict(dry_run=True, max=None, failure_limit=2, json=False, board=None)
    base.update(over)
    return argparse.Namespace(**base)


def _stub_presence(monkeypatch, value):
    import hermes_cli.kanban as kanban_cli

    monkeypatch.setattr(kanban_cli, "_check_dispatcher_presence", lambda *a, **k: value)


def _capture_dispatch(monkeypatch):
    calls: list[dict] = []

    def fake_dispatch_once(conn, **kwargs):
        calls.append(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    return calls


def test_dispatch_is_refused_beside_a_live_gateway_dispatcher(home, monkeypatch, capsys):
    from hermes_cli import kanban_ops

    _stub_presence(monkeypatch, LIVE)
    calls = _capture_dispatch(monkeypatch)

    rc = kanban_ops._cmd_dispatch(_args())
    err = capsys.readouterr().err

    assert rc == 2, "a second driver must fail, not warn"
    assert calls == [], "nothing may be dispatched when the refusal fires"
    assert err.startswith("kanban dispatch refused: gateway pid=4904, dispatch enabled"), err
    assert "--force" in err, "the refusal must name the documented override"


def test_force_dispatches_beside_a_live_gateway_dispatcher(home, monkeypatch, capsys):
    from hermes_cli import kanban_ops

    _stub_presence(monkeypatch, LIVE)
    calls = _capture_dispatch(monkeypatch)

    rc = kanban_ops._cmd_dispatch(_args(force=True))
    out = capsys.readouterr().out

    assert rc == 0
    assert len(calls) == 1, "--force is the deliberate one-off; it must actually tick"
    assert "Caps: max_in_progress=2 (config)" in out


def test_no_gateway_means_no_refusal(home, monkeypatch):
    from hermes_cli import kanban_ops

    _stub_presence(monkeypatch, (False, "No gateway is running — the task will sit in 'ready' until you start it."))
    calls = _capture_dispatch(monkeypatch)

    assert kanban_ops._cmd_dispatch(_args()) == 0
    assert len(calls) == 1, "a home with no gateway still needs its manual dispatch"


def test_an_unanswerable_probe_does_not_refuse(home, monkeypatch):
    """``_check_dispatcher_presence`` fails OPEN with ``(True, "")``: can't-tell is not a refusal."""
    from hermes_cli import kanban_ops

    _stub_presence(monkeypatch, (True, ""))
    calls = _capture_dispatch(monkeypatch)

    assert kanban_ops._cmd_dispatch(_args()) == 0
    assert len(calls) == 1


def test_dispatch_in_gateway_false_is_not_a_live_dispatcher(home, monkeypatch):
    """The gateway being up with ``dispatch_in_gateway: false`` is not a second driver."""
    from hermes_cli import kanban_ops

    _stub_presence(monkeypatch, (False, "Gateway is running but kanban.dispatch_in_gateway=false"))
    calls = _capture_dispatch(monkeypatch)

    assert kanban_ops._cmd_dispatch(_args()) == 0
    assert len(calls) == 1


def test_the_parser_exposes_the_override():
    import argparse as _argparse

    from hermes_cli import kanban as kc

    parser = _argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    args = parser.parse_args(["kanban", "dispatch", "--force", "--dry-run"])
    assert args.force is True
    assert args.kanban_action == "dispatch"
