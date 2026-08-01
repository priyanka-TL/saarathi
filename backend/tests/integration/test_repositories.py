import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy.exc import IntegrityError

from app.database.engine import engine, SessionLocal
from app.domain.core import UserContext
from app.repositories.conversations import ConversationRepository


def _user(tenant_code: str, user_id: str) -> UserContext:
    """UserContext requires email and display_name. Every call site in this file
    omitted them, so all of these tests died in setup with a TypeError long
    before asserting anything."""
    return UserContext(
        user_id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        tenant_code=tenant_code,
    )


@pytest.fixture
def db_session():
    """
    Yields a session. We don't use transaction rollback here because we need 
    to test cross-thread row locks which require actual commits.
    Data is isolated by generating unique UUIDs for each test.
    """
    session = SessionLocal()
    yield session
    session.close()

def test_cross_tenant_id_behaves_as_not_found_and_creates_a_fresh_conversation(db_session):
    """A conversation id the caller does not own is a MISS, and a miss creates a
    conversation with a server-generated id.

    This used to reuse the caller-supplied id as the new row's primary key,
    which had two bad consequences. Cross-tenant it collided with the existing
    row and surfaced as an IntegrityError 500 (which is what this test used to
    assert). Same-tenant-but-unknown -- a stale sessionStorage.saarthi_cid after
    a different login or a rebuilt database -- it silently resurrected the dead
    id as a live conversation, making the primary key a client-controlled value.
    """
    repo = ConversationRepository(db_session)

    # 1. Create a conversation for Tenant A
    user_a = _user("TENANT_A", "user_a")
    conv_a = repo.get_or_create(None, user_a)
    db_session.commit()

    # 2. Ask for the SAME conversation ID as Tenant B.
    user_b = _user("TENANT_B", "user_b")
    conv_b = repo.get_or_create(conv_a.id, user_b)
    db_session.commit()

    assert conv_b.id != conv_a.id, "the caller's id was reused as the primary key"
    assert conv_b.tenant_code == "TENANT_B"

    # Tenant A's conversation is untouched and still theirs.
    still_a = repo.get_scoped(conv_a.id, user_a)
    assert still_a is not None
    assert still_a.tenant_code == "TENANT_A"
    # ...and remains invisible to Tenant B.
    assert repo.get_scoped(conv_a.id, user_b) is None


def test_unknown_id_for_the_owner_also_gets_a_fresh_id(db_session):
    """The stale-sessionStorage case: same user, an id that no longer exists."""
    repo = ConversationRepository(db_session)
    user = _user("TENANT_STALE", "user_stale")

    dead_id = uuid.uuid4()
    conv = repo.get_or_create(dead_id, user)
    db_session.commit()

    assert conv.id != dead_id, "a client-supplied id must never become the primary key"


def test_concurrent_next_seq_for_update_serialises():
    # Setup: Create a conversation and commit it
    session = SessionLocal()
    repo = ConversationRepository(session)
    user = _user("TENANT_CONC", "user_conc")
    conv = repo.get_or_create(None, user)
    session.commit()
    session.close()

    # The function that each thread will run
    def get_next_seq(conv_id):
        # Must use a separate session and connection for true concurrency
        thread_session = SessionLocal()
        thread_repo = ConversationRepository(thread_session)
        try:
            seq = thread_repo.next_seq_for_update(conv_id)
            thread_session.commit()
            return seq
        finally:
            thread_session.close()

    # Run 10 concurrent requests
    workers = 10
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(get_next_seq, conv.id) for _ in range(workers)]
        results = [f.result() for f in futures]

    # Verification: There should be exactly 'workers' unique sequences, from 1 to 10.
    assert len(results) == workers, "Should return a result for each thread"
    assert set(results) == set(range(1, workers + 1)), "Should produce exactly 1 to N with no duplicates or gaps"
