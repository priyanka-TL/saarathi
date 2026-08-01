"""The advisory lock, exercised through the real HTTP stack.

Flask's dev server serialised requests, so this could never be tested
end-to-end there. Under a 16-thread worker pool it is the single most important
proof that turn semantics survived the port: two simultaneous turns on one
conversation must produce exactly one 200 and one 409, never two 200s.

Why it matters beyond tidiness: Mitra merges consecutive messages from the same
sender, so a second turn admitted while the first is in flight does not produce
a duplicate -- it destroys an answer, silently, with no error anywhere.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal


def test_two_simultaneous_turns_yield_one_200_and_one_409(client, script):
    """Both requests target the same conversation; the loser must be refused."""
    # Each turn costs two scripted calls (router classifier, then the agent).
    # Queue generously: only one of the two racers is admitted, but which one
    # gets there first is what we are deliberately not controlling.
    for _ in range(8):
        script.queue("reply")

    # Establish the conversation so both racers address the same id.
    seed = client.post("/api/chat", json={"message": "hello"})
    assert seed.status_code == 200
    conversation_id = seed.json()["conversation_id"]

    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def fire():
        barrier.wait(timeout=10)          # maximise the overlap
        r = client.post(
            "/api/chat",
            json={"message": "racing turn", "conversation_id": conversation_id},
        )
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == [200, 409], (
        f"expected exactly one 200 and one 409, got {sorted(results)}. "
        "Either the advisory lock is not being taken, or it is not being held "
        "across the handler call."
    )


def test_advisory_lock_is_released_after_a_turn(client, script):
    """A session-scoped lock is NOT released by commit, rollback, or returning
    the connection to the pool -- only by an explicit pg_advisory_unlock on the
    same connection.

    If the unlock ever silently no-ops (the classic symptom of the Session
    changing connection mid-turn), the FIRST turn succeeds and every subsequent
    turn on that conversation 409s forever. Asserting on pg_locks catches that
    directly rather than waiting for it to show up as a stuck conversation.
    """
    # Two turns, and each turn costs TWO scripted calls: the router's gate-4
    # classifier and then the agent itself.
    for _ in range(6):
        script.queue("reply")

    first = client.post("/api/chat", json={"message": "hello"})
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]

    # Scoped to THIS database: pg_locks is cluster-wide, and a dev server
    # running against the non-test database would otherwise show up here.
    with SessionLocal() as db:
        held = db.execute(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' "
                "  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            )
        ).scalar()
    assert held == 0, (
        f"{held} advisory lock(s) still held after the turn completed. "
        "This is the signature of the unlock landing on a different pooled "
        "connection than the lock -- see app/dependencies/db.py."
    )

    # And the proof that matters to a user: the next turn is admitted.
    second = client.post(
        "/api/chat", json={"message": "again", "conversation_id": conversation_id}
    )
    assert second.status_code == 200
