"""The git binary Hermes actually runs, chosen by running it.

A name on PATH is not an answer. On Windows the bundled ``git.EXE`` can live under
an ACL that denies ``CreateProcess``: the file exists, ``shutil.which`` returns it,
and every spawn raises ``PermissionError [WinError 5]``. A caller that reads that
failure as "git said no" then blames the user's directory for the tool's absence —
a valid git root reads as "not a git repo" and a worktree card dies on its retries.

So every candidate is asked ``--version``, in order; one that cannot run is recorded
and skipped, and the first that runs wins. The winner is remembered for the process
and written to ``<HERMES_HOME>/state/git.json`` as ``git: <path> <version> @<minute>``
so another component can read what ran instead of resolving its own answer.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass

from hermes_platform.resolver.core import CheckState

_RECORD_NAME = "git.json"
_CACHE: GitBinary | None = None


@dataclass(frozen=True)
class GitAttempt:
    """One candidate and what running it answered."""

    path: str
    source: str
    state: CheckState
    detail: str = ""


@dataclass(frozen=True)
class GitBinary:
    """The git that ran, or every reason none did."""

    path: str | None
    version: str = ""
    attempts: tuple[GitAttempt, ...] = ()

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def unavailable_reason(self) -> str:
        """The first execution failure, spelled the way the OS spelled it."""
        for attempt in self.attempts:
            if attempt.state is CheckState.ERROR and attempt.detail:
                return attempt.detail
        return "no git executable could be run from PATH or a known location"

    @property
    def line(self) -> str:
        return f"git: {self.path} {self.version} @{_minute()}"


def _minute() -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())


def _detail(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror:
        return f"WinError {winerror}: {getattr(exc, 'strerror', None) or exc}"
    return f"{exc.__class__.__name__}: {exc}"


def _names() -> tuple[str, ...]:
    """The file names a git binary carries on this OS, launcher-friendly first."""
    if os.name != "nt":
        return ("git",)
    exts = [e for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep) if e]
    return tuple(f"git{ext}" for ext in exts) + ("git",)


def _known_dirs() -> tuple[str, ...]:
    if os.name == "nt":
        roots = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "mingw64", "bin"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "cmd"),
        ]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(os.path.join(local, "Programs", "Git", "cmd"))
        return tuple(roots)
    return ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin", "/bin")


def _candidates() -> list[tuple[str, str]]:
    """(path, source) for every plausible git, in the order it should be tried.

    The recorded winner goes first as an ordering hint only: it still has to run,
    so a record left by an upgrade that moved the binary costs one probe, not a
    wrong answer.
    """
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: str, source: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            ordered.append((path, source))

    record = read_git_record()
    if record and record.get("host") == _host() and record.get("path"):
        add(str(record["path"]), "record")
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in _names():
            add(os.path.join(directory, name), "PATH")
    for directory in _known_dirs():
        for name in _names():
            add(os.path.join(directory, name), "known_dir")
    return ordered


def resolve_git(*, refresh: bool = False) -> GitBinary:
    """The first candidate whose ``--version`` runs; the failures otherwise."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    attempts: list[GitAttempt] = []
    for path, source in _candidates():
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, check=False, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            attempts.append(GitAttempt(path, source, CheckState.ERROR, _detail(exc)))
            continue
        stdout = (result.stdout or "").strip()
        if result.returncode != 0 or not stdout.startswith("git version"):
            seen = (result.stderr or "").strip().splitlines()
            attempts.append(
                GitAttempt(path, source, CheckState.ERROR, seen[0] if seen else f"exit {result.returncode}")
            )
            continue
        attempts.append(GitAttempt(path, source, CheckState.PRESENT, stdout))
        winner = GitBinary(path, stdout.removeprefix("git version").strip(), tuple(attempts))
        _CACHE = winner
        write_git_record(winner)
        return winner
    _CACHE = GitBinary(None, "", tuple(attempts))
    return _CACHE


def _host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _record_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / _RECORD_NAME


def read_git_record() -> dict | None:
    """What last ran on this host, or ``None`` — evidence, never a decision."""
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_git_record(git: GitBinary) -> None:
    """Best-effort receipt for the rest of the fleet. A read-only home is not an error."""
    try:
        path = _record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"host": _host(), "path": git.path, "version": git.version,
                   "line": git.line, "epoch": int(time.time())}
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(staging, path)
    except OSError:
        pass
