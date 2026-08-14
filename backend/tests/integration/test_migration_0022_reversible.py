"""Migration 0022 goes down as cleanly as it goes up.

WHY A DOWNGRADE IS WORTH A TEST AT ALL. An upgrade is exercised by every other
test in this suite -- the shared test database is migrated to head before any of
them run, so a broken `upgrade()` fails everything at once and loudly. A broken
`downgrade()` fails nothing until someone needs it, which is during an incident.

AND 0022's DOWNGRADE IS THE KIND THAT BREAKS. `Base.metadata`'s naming
convention interpolates `%(constraint_name)s` and re-applies it, so:

  - `op.create_check_constraint("ws_timings", ...)` produces
    `ck_conversation_messages_ws_timings` -- a BARE suffix goes in, a qualified
    name comes out;
  - `op.drop_constraint("ck_conversation_messages_ws_timings", ...)` runs that
    ALREADY-QUALIFIED name through the same convention and looks for
    `ck_conversation_messages_ck_conversation_messages_ws_timings`, which does
    not exist.

So the downgrade drops its constraints with RAW SQL. Both traps are 0014's, and
both are documented there and in 0022 -- but a comment is not a check, and the
mistake reads as correct in review.

RUNS IN A SUBPROCESS AGAINST A THROWAWAY DATABASE. Downgrading the shared test
database mid-suite would leave every later test running against a schema one
revision behind, and a failure halfway through would leave it there permanently.
A separate database costs a few seconds and cannot corrupt anything.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy as sa

BACKEND = Path(__file__).resolve().parents[2]

#: The four columns and three constraints 0022 adds, which 0021 must not have.
COLUMNS = ("ws_end_reason", "ws_first_frame_ms", "ws_last_frame_ms", "ws_fragments")
CONSTRAINTS = (
    "ck_conversation_messages_ws_end_reason",
    "ck_conversation_messages_ws_only_assistant",
    "ck_conversation_messages_ws_timings",
)


def _scratch_url() -> str:
    """A database name nothing else will ever use."""
    parsed = urlparse(os.environ["DATABASE_URL"])
    return urlunparse(parsed._replace(path=f"/mig22_{uuid.uuid4().hex[:12]}"))


def _admin_engine(url: str):
    parsed = urlparse(url)
    return sa.create_engine(
        urlunparse(parsed._replace(path="/postgres")), isolation_level="AUTOCOMMIT",
    )


def _alembic(url: str, *args: str) -> None:
    """Alembic in a subprocess, so `DATABASE_URL` can differ from this process's.

    `migrations/env.py` reads the URL from `Settings` at import time and
    overwrites whatever the config carries, so pointing it elsewhere means
    pointing the ENVIRONMENT elsewhere -- which is only safe in a child.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )


@pytest.fixture()
def scratch_db():
    url = _scratch_url()
    name = urlparse(url).path.lstrip("/")

    admin = _admin_engine(url)
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        yield url
    finally:
        with admin.connect() as conn:
            # Terminate first: alembic's own connection may linger just long
            # enough for the DROP to fail, which would leak a database per run.
            conn.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ), {"n": name})
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _columns(url: str) -> set:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "select column_name from information_schema.columns "
                "where table_name = 'conversation_messages'"
            )).scalars().all()
    finally:
        engine.dispose()
    return set(rows)


def _constraints(url: str) -> set:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "select conname from pg_constraint "
                "where conrelid = 'conversation_messages'::regclass"
            )).scalars().all()
    finally:
        engine.dispose()
    return set(rows)


def test_0022_can_be_applied_reverted_and_reapplied(scratch_db):
    """UP, DOWN, UP. The third step is the one that catches a downgrade which
    "succeeded" without actually dropping anything: re-applying would then fail
    on a duplicate column or constraint."""
    _alembic(scratch_db, "upgrade", "0021")

    at_0021_columns = _columns(scratch_db)
    assert not (at_0021_columns & set(COLUMNS)), (
        "0021 already has 0022's columns -- the revision chain is wrong"
    )

    _alembic(scratch_db, "upgrade", "0022")

    assert set(COLUMNS) <= _columns(scratch_db)
    # The CHECKs land under their BARE suffix, prefixed exactly once. A second
    # `ck_conversation_messages_` here is the naming trap in its upgrade form.
    assert set(CONSTRAINTS) <= _constraints(scratch_db)

    _alembic(scratch_db, "downgrade", "0021")

    # THE ASSERTION THE RAW-SQL DROP EXISTS FOR. `op.drop_constraint` would have
    # raised "constraint does not exist" before reaching the columns.
    assert not (_columns(scratch_db) & set(COLUMNS))
    assert not (_constraints(scratch_db) & set(CONSTRAINTS))
    assert _columns(scratch_db) == at_0021_columns, (
        "the downgrade did not land back on 0021's shape"
    )

    _alembic(scratch_db, "upgrade", "0022")

    assert set(COLUMNS) <= _columns(scratch_db)
    assert set(CONSTRAINTS) <= _constraints(scratch_db)
