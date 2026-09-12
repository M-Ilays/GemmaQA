"""The database must resolve to the same file no matter the launch directory.

`database_url`'s default, `sqlite+aiosqlite:///./gemmaqa.db`, is a RELATIVE
path. SQLAlchemy resolves a relative sqlite path against the process's current
working directory, not the project — so starting the backend from `backend/`
one day and from the project root the next silently produces two different
database files with two unrelated run histories.

Measured live before the fix: 334 runs in `backend/gemmaqa.db`, 631 runs in
`gemmaqa.db` at the project root, zero overlapping run ids, each file
internally consistent and each one "correct" from inside the process that
wrote it. The two were merged back into one file by hand (965 runs, verified
`PRAGMA integrity_check` = ok, zero duplicate ids). This test is what stops it
from splitting again.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402

EXPECTED_PATH = BACKEND.parent / "gemmaqa.db"


def test_the_default_resolves_to_the_project_root_not_the_cwd():
    settings = Settings()
    assert settings.effective_database_url == (
        f"sqlite+aiosqlite:///{EXPECTED_PATH.as_posix()}"
    )


def test_the_resolved_path_does_not_depend_on_cwd(tmp_path, monkeypatch):
    """The regression itself: launch from two different directories, get the
    same answer both times."""
    monkeypatch.chdir(tmp_path)
    from_elsewhere = Settings().effective_database_url

    monkeypatch.chdir(BACKEND)
    from_backend = Settings().effective_database_url

    assert from_elsewhere == from_backend
    assert from_elsewhere == f"sqlite+aiosqlite:///{EXPECTED_PATH.as_posix()}"


def test_an_operator_supplied_url_is_never_overridden():
    """The rewrite must fire ONLY on the untouched default. An operator who set
    DATABASE_URL to a different sqlite path, or a real server, must get exactly
    that string back — silently redirecting a deliberate choice would be worse
    than the bug this fixes."""
    custom = "postgresql+asyncpg://user:pass@host/gemmaqa"
    settings = Settings(database_url=custom)
    assert settings.effective_database_url == custom

    custom_sqlite = "sqlite+aiosqlite:///./somewhere/else.db"
    settings = Settings(database_url=custom_sqlite)
    assert settings.effective_database_url == custom_sqlite


def test_database_py_uses_the_effective_url_not_the_raw_field():
    """The property is useless if the one call site does not read it."""
    import inspect

    from app import database

    source = inspect.getsource(database)
    assert "settings.effective_database_url" in source
    assert "create_async_engine(\n    settings.database_url," not in source
