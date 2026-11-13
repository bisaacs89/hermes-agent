"""Regression: a git that cannot run is "unread", never "not a git repo".

Repro (a host where a bundled git cannot be spawned): ``shutil.which("git")``
answered a bundled ``agent-payload/.../git.EXE`` whose ``CreateProcess`` raises
``PermissionError [WinError 5]``. ``_git_out`` swallowed that and returned ``None``,
``_git_toplevel`` read ``None`` as "not in a repo", and the dispatcher refused a
directory that is a valid git root:

    workspace: worktree path ... is not inside a git repo

A probe that reads an execution failure as a negative answer refuses every worktree
card on such a host, on every retry, with a refusal that was never about the
directory. The probe has three answers — a git root, not a git root, and could not
tell — and only the middle one is a refusal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_platform.resolver import git as git_resolver
from hermes_platform.resolver.core import CheckState


class _Task:
    id = "t_probe"
    workspace_kind = "worktree"
    branch_name = None

    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path


def _git() -> str:
    found = git_resolver.resolve_git(refresh=True)
    if not found.available:
        pytest.skip(f"no runnable git on this host: {found.unavailable_reason}")
    return str(found.path)


def _init_repo(repo: Path) -> None:
    git = _git()
    repo.mkdir()
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    for args in (
        ["init", "-b", "main", str(repo)],
        ["-C", str(repo), "add", "README.md"],
        ["-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-m", "init"],
    ):
        assert subprocess.run([git, *args], capture_output=True, timeout=120).returncode == 0, args


def _unrunnable() -> git_resolver.GitBinary:
    """What the resolver returns when the only git named is one that cannot spawn."""
    return git_resolver.GitBinary(
        None, "", (git_resolver.GitAttempt("C:/payload/git.EXE", "PATH", CheckState.ERROR,
                                           "WinError 5: Access is denied"),)
    )


def test_unrunnable_git_reads_unread_not_not_a_repo(tmp_path, monkeypatch):
    """A real git root, and a git that exists but cannot run."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(kb, "resolve_git", lambda **_: _unrunnable())

    with pytest.raises(kb.GitUnread) as caught:
        kbw._resolve_worktree_workspace(_Task(str(repo)))

    message = str(caught.value)
    assert "unread — git unavailable: WinError 5: Access is denied" in message
    assert "not inside a git repo" not in message
    assert "not a git repo" not in message


def test_unrunnable_git_surfaces_through_every_worktree_entry_point(tmp_path, monkeypatch):
    """Both shapes a card can name — an existing root and a not-yet-made target."""
    monkeypatch.setattr(kb, "resolve_git", lambda **_: _unrunnable())
    existing = tmp_path / "existing"
    existing.mkdir()
    target = tmp_path / "future" / "worktree"

    for workspace_path in (str(existing), str(target)):
        with pytest.raises(kb.GitUnread) as caught:
            kbw._resolve_worktree_workspace(_Task(workspace_path))
        assert "unread — git unavailable" in str(caught.value)


def test_a_directory_outside_any_repo_still_refuses(tmp_path):
    """The middle state is unchanged: git ran, answered no, and the card is refused."""
    _git()
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ValueError) as caught:
        kbw._resolve_worktree_workspace(_Task(str(plain)))

    message = str(caught.value)
    assert "is not inside a git repo" in message
    assert "unread" not in message


def test_a_valid_repo_root_resolves_to_its_worktree(tmp_path):
    """The first state: a real repo resolves, so the fix did not harden the good path."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    resolved, branch = kbw._resolve_worktree_workspace(_Task(str(repo)))

    expected = repo / ".worktrees" / _Task.id
    assert Path(resolved).resolve() == expected.resolve()
    assert expected.is_dir()
    assert branch == f"wt/{_Task.id}"


def test_a_candidate_that_cannot_spawn_falls_through(tmp_path, monkeypatch):
    """A named-but-unspawnable binary is skipped, not raised: the next one that runs wins."""
    real = _git()
    unspawnable = tmp_path / ("git.EXE" if git_resolver.os.name == "nt" else "git")
    unspawnable.write_text("not a program\n", encoding="utf-8")
    monkeypatch.setattr(git_resolver, "_candidates",
                        lambda: [(str(unspawnable), "PATH"), (real, "PATH")])

    found = git_resolver.resolve_git(refresh=True)

    assert found.path == real
    assert found.attempts[0].path == str(unspawnable)
    assert found.attempts[0].state is CheckState.ERROR
    assert found.attempts[0].detail


def test_nothing_runnable_reports_the_execution_failure(tmp_path, monkeypatch):
    """No candidate runs: the reason is the OS's execution error, phrased as itself."""
    unspawnable = tmp_path / "git"
    unspawnable.write_text("not a program\n", encoding="utf-8")
    monkeypatch.setattr(git_resolver, "_candidates", lambda: [(str(unspawnable), "PATH")])

    found = git_resolver.resolve_git(refresh=True)

    assert not found.available
    assert found.attempts[0].state is CheckState.ERROR
    assert found.unavailable_reason == found.attempts[0].detail
    assert found.unavailable_reason != "no git executable could be run from PATH or a known location"


def test_the_winner_is_recorded_with_what_ran(tmp_path):
    """One receipt — ``git: <path> <version> @<minute>`` — that other components can read."""
    real = _git()

    found = git_resolver.resolve_git(refresh=True)
    record = git_resolver.read_git_record()

    assert record is not None
    assert record["path"] == real
    assert record["version"] == found.version
    assert record["line"].startswith(f"git: {real} ")
    assert record["line"].endswith("Z")


def test_a_denied_spawn_at_run_time_is_unread_not_a_negative_answer(tmp_path, monkeypatch):
    """The resolver's winner can still be denied later; that is not git saying "no"."""
    winner = git_resolver.GitBinary("C:/denied/git.EXE", "2.53.0")

    def denied(*_args, **_kwargs):
        raise OSError("spawn refused")

    monkeypatch.setattr(kb, "resolve_git", lambda **_: winner)
    monkeypatch.setattr(kb.subprocess, "run", denied)

    with pytest.raises(kb.GitUnread) as caught:
        kb._git_out(tmp_path, "rev-parse", "--show-toplevel")

    assert "unread — git unavailable: OSError: spawn refused" in str(caught.value)


def test_exec_detail_names_the_windows_error_the_way_windows_does():
    """``WinError 5`` reads as itself in the refusal, not as a claim about the directory."""
    assert "Access is denied" in kb._exec_detail(PermissionError(13, "Access is denied"))
