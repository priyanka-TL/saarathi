"""Unit tests for ChannelPool against a fake channel factory.

No real sockets. Reap intervals/idle thresholds are kept small (tens of
milliseconds) so the suite stays fast while still exercising real
wall-clock timing logic for the idle reaper.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Tuple

from app.providers.connection import RemoteConnection
from app.providers.errors import ProviderChannelClosed
from app.providers.transport.pool import ChannelPool


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    conversation_id: uuid.UUID
    remote_session_id: str = "remote-1"


@dataclass
class _Spec:
    flow_name: str = "guest-mi-story"


def _pool(factory, *, max_open: int = 200, idle_close_s: float = 1200.0,
          reap_interval_s: float = 1000.0) -> ChannelPool:
    """A pool wired to `factory`.

    THE FACTORY IS PER CALL on the real pool, not held on it: a pool is
    process-wide per provider, but building a channel needs the caller's
    resolved connection and its credentials. `_acquire` below closes over them,
    which is exactly what BaseWsFlowProvider.turn does.
    """
    return ChannelPool(
        max_open=max_open, idle_close_s=idle_close_s,
        name="test", reap_interval_s=reap_interval_s,
    )


def _acquire(pool: ChannelPool, factory, spec, sess, conn):
    return pool.acquire(sess, conn, lambda: factory(spec, sess, conn))


def _reacquire(pool: ChannelPool, factory, spec, sess, conn):
    return pool.reacquire(sess, conn, lambda: factory(spec, sess, conn))


class _FakeChannel:
    def __init__(self, spec, sess, conn):
        self.spec = spec
        self.sess = sess
        self.conn = conn
        # WsChannel stamps the connection it was opened against; acquire()
        # compares it to decide whether a pooled channel is still valid.
        self.conn_checksum = conn.checksum
        self.alive = True
        self.close_calls: List[bool] = []

    def close(self, quiet: bool = False) -> None:
        self.alive = False
        self.close_calls.append(quiet)


class _FakeChannelFactory:
    def __init__(self):
        self.calls: List[Tuple] = []
        self.channels: List[_FakeChannel] = []

    def __call__(self, spec, sess, conn) -> _FakeChannel:
        self.calls.append((spec, sess, conn))
        ch = _FakeChannel(spec, sess, conn)
        self.channels.append(ch)
        return ch


def _conn(**overrides) -> RemoteConnection:
    """The real value object -- it is pure, and its checksum is the thing under
    test in the staleness cases below."""
    defaults = dict(
        provider="testprovider",
        base_url="https://remote.example.com",
        stream_url="wss://remote.example.com/ws/common/",
        user_agent="test-agent",
    )
    defaults.update(overrides)
    return RemoteConnection(**defaults)


_CONN = _conn()


def _new_sess(remote_session_id: str = "remote-1") -> _Session:
    return _Session(conversation_id=uuid.uuid4(), remote_session_id=remote_session_id)


# ---------------------------------------------------------------------------
# Acceptance 1: a miss re-authenticates with the stored session id and resumes
# ---------------------------------------------------------------------------


def test_miss_reauthenticates_with_stored_session_id_and_resumes():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess = _new_sess(remote_session_id="remote-abc")

        ch1 = _acquire(mgr, factory, spec, sess, _CONN)
        assert len(factory.calls) == 1
        assert factory.calls[0][1].remote_session_id == "remote-abc"

        # Simulate a connection loss detected elsewhere.
        ch1.alive = False

        # Caller re-reads the session from Postgres (same remote_session_id
        # here, since Mitra's own DB -- not the socket -- holds the real
        # interview state) and calls acquire() again.
        ch2 = _acquire(mgr, factory, spec, sess, _CONN)
        assert len(factory.calls) == 2
        assert factory.calls[1][1].remote_session_id == "remote-abc"
        assert ch2 is not ch1
    finally:
        mgr.stop_reaper()


def test_hit_returns_same_channel_without_reauthenticating():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = _acquire(mgr, factory, spec, sess, _CONN)
        ch2 = _acquire(mgr, factory, spec, sess, _CONN)
        assert ch1 is ch2
        assert len(factory.calls) == 1
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Acceptance 2: LRU eviction respects the bound; evicted channels are closed
# ---------------------------------------------------------------------------


def test_lru_eviction_respects_bound_and_closes_evicted():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, max_open=2, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()

        ch1 = _acquire(mgr, factory, spec, sess1, _CONN)
        ch2 = _acquire(mgr, factory, spec, sess2, _CONN)
        ch3 = _acquire(mgr, factory, spec, sess3, _CONN)  # bound is 2 -> evicts ch1 (least recently used)

        assert len(mgr._entries) == 2
        assert ch1.alive is False
        assert ch1.close_calls == [True]

        # ch2/ch3 remain hits -- no new factory calls.
        assert _acquire(mgr, factory, spec, sess2, _CONN) is ch2
        assert _acquire(mgr, factory, spec, sess3, _CONN) is ch3
        assert len(factory.calls) == 3
    finally:
        mgr.stop_reaper()


def test_lru_touch_on_hit_changes_eviction_order():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, max_open=2, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()

        ch1 = _acquire(mgr, factory, spec, sess1, _CONN)
        ch2 = _acquire(mgr, factory, spec, sess2, _CONN)
        _acquire(mgr, factory, spec, sess1, _CONN)  # touch sess1 -> now sess2 is least recently used

        ch3 = _acquire(mgr, factory, spec, sess3, _CONN)  # should evict sess2, not sess1

        assert ch2.alive is False
        assert ch1.alive is True
        assert len(mgr._entries) == 2
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# A pooled channel is only a hit while its CONNECTION is still current
# ---------------------------------------------------------------------------


def test_reconfigured_scope_gets_a_fresh_channel_not_the_pooled_one():
    """This is what makes a runtime config change take effect.

    Without it a live channel keeps talking to the old Mitra instance, as the
    old company, with the old Origin, until the idle reaper happens to close it
    -- up to mitra_idle_close_s (20 minutes by default) later.
    """
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = _acquire(mgr, factory, spec, sess, _CONN)

        moved = _conn(base_url="https://other-mitra.example.com")
        assert moved.checksum != _CONN.checksum

        ch2 = _acquire(mgr, factory, spec, sess, moved)

        assert ch2 is not ch1
        assert ch1.alive is False, "the stale channel must be closed, not leaked"
        assert ch1.close_calls == [True]
        assert factory.calls[1][2] is moved
    finally:
        mgr.stop_reaper()


def test_unchanged_connection_is_still_a_hit():
    """The staleness check must compare CONTENT, not object identity -- a
    connection is re-resolved on every turn, so a fresh but equal instance
    must not force a reconnect."""
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = _acquire(mgr, factory, spec, sess, _conn())
        ch2 = _acquire(mgr, factory, spec, sess, _conn())  # equal content, different object

        assert ch1 is ch2
        assert len(factory.calls) == 1
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# reacquire / close / close_all
# ---------------------------------------------------------------------------


def test_reacquire_always_creates_fresh_channel_even_if_alive():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = _acquire(mgr, factory, spec, sess, _CONN)
        assert ch1.alive is True

        ch2 = _reacquire(mgr, factory, spec, sess, _CONN)
        assert ch2 is not ch1
        assert ch1.close_calls == [True]
        assert len(factory.calls) == 2
        assert _acquire(mgr, factory, spec, sess, _CONN) is ch2  # the fresh one is now cached
    finally:
        mgr.stop_reaper()


def test_close_removes_and_closes_only_that_conversation():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2 = _new_sess(), _new_sess()
        ch1 = _acquire(mgr, factory, spec, sess1, _CONN)
        ch2 = _acquire(mgr, factory, spec, sess2, _CONN)

        mgr.close(sess1.conversation_id)

        assert ch1.alive is False
        assert ch2.alive is True
        assert sess1.conversation_id not in mgr._entries
        assert sess2.conversation_id in mgr._entries
    finally:
        mgr.stop_reaper()


def test_close_on_unknown_conversation_id_is_a_no_op():
    mgr = _pool(_FakeChannelFactory(), reap_interval_s=1000)
    try:
        mgr.close(uuid.uuid4())  # must not raise
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Acceptance 3: atexit closes all channels
# ---------------------------------------------------------------------------


def test_close_all_closes_every_channel():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()
        chans = [_acquire(mgr, factory, spec, s, _CONN) for s in (sess1, sess2, sess3)]

        mgr.close_all()

        assert all(ch.alive is False for ch in chans)
        assert all(ch.close_calls == [True] for ch in chans)
        assert len(mgr._entries) == 0
    finally:
        mgr.stop_reaper()


def test_atexit_registers_close_all(monkeypatch):
    registered = {}

    def fake_register(fn, *args, **kwargs):
        registered["fn"] = fn

    monkeypatch.setattr("app.providers.transport.pool.atexit.register", fake_register)

    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)
    try:
        assert "fn" in registered

        spec, sess = _Spec(), _new_sess()
        ch = _acquire(mgr, factory, spec, sess, _CONN)

        registered["fn"]()  # simulate process exit invoking the registered hook

        assert ch.alive is False
        assert len(mgr._entries) == 0
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------


def test_idle_reaper_closes_channels_idle_beyond_threshold():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, idle_close_s=0.05, reap_interval_s=0.02)
    try:
        spec, sess = _Spec(), _new_sess()
        ch = _acquire(mgr, factory, spec, sess, _CONN)
        assert ch.alive is True

        time.sleep(0.2)

        assert ch.alive is False
        assert sess.conversation_id not in mgr._entries
    finally:
        mgr.stop_reaper()


def test_idle_reaper_cleans_up_dead_channels_before_idle_threshold():
    factory = _FakeChannelFactory()
    mgr = _pool(factory, idle_close_s=1000.0, reap_interval_s=0.02)  # long idle threshold
    try:
        spec, sess = _Spec(), _new_sess()
        ch = _acquire(mgr, factory, spec, sess, _CONN)
        ch.alive = False  # died externally, well before the idle threshold

        time.sleep(0.1)

        assert sess.conversation_id not in mgr._entries
    finally:
        mgr.stop_reaper()


def test_reaper_thread_is_daemon():
    mgr = _pool(_FakeChannelFactory(), reap_interval_s=1000)
    try:
        assert mgr._reaper.daemon is True
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Concurrency: the pool lock covers METADATA ONLY
#
# Both tests below are deterministic -- a Barrier or an Event decides the
# interleaving, never a sleep. A wall-clock assertion ("finished in ~1s, not
# ~2s") would prove the same thing and fail randomly on a loaded CI box.
# ---------------------------------------------------------------------------


def test_different_conversations_build_concurrently():
    """Two cold starts must be inside factory() AT THE SAME TIME.

    Fails against a pool that calls the factory under its own lock: the second
    thread cannot reach the barrier until the first has returned, so the barrier
    times out. That serialisation is invisible in production except as latency --
    N simultaneous first turns cost N handshakes end to end, and one unreachable
    remote stalls every other conversation for its full connect timeout.
    """
    both_inside = threading.Barrier(2, timeout=5)
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)

    def blocking_factory(spec, sess, conn):
        both_inside.wait()  # raises BrokenBarrierError on timeout
        return factory(spec, sess, conn)

    errors: List[BaseException] = []

    def run(sess):
        try:
            _acquire(mgr, blocking_factory, _Spec(), sess, _CONN)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=run, args=(_new_sess(),)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"builds did not overlap: {errors!r}"
        assert len(factory.calls) == 2
    finally:
        mgr.stop_reaper()


def test_close_during_build_does_not_leave_a_duplicate_channel():
    """One conversation must never build twice, even across a close().

    THE REGRESSION THIS PINS. A build lock kept in a dict of its own would be
    dropped by close() here -- the conversation is absent from the channel map
    for the whole build, because acquire() took the stale channel out and has
    not put a new one in -- so the second caller would mint a fresh lock and open
    a SECOND socket, with nothing raised and nothing logged. Refcounting the
    entry (`waiters`) is what makes that impossible; see ChannelPool._maybe_drop.

    close() landing mid-build must also still take effect: before the factory
    moved out from under the pool lock, close() simply waited and then closed the
    channel, so a conversation that was closed must not come back holding a live
    socket.
    """
    in_factory = threading.Event()
    release = threading.Event()
    factory = _FakeChannelFactory()
    mgr = _pool(factory, reap_interval_s=1000)

    def blocking_factory(spec, sess, conn):
        in_factory.set()
        release.wait(timeout=5)
        return factory(spec, sess, conn)

    sess = _new_sess()
    outcome: List[BaseException] = []

    def build():
        try:
            _acquire(mgr, blocking_factory, _Spec(), sess, _CONN)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    try:
        builder = threading.Thread(target=build)
        builder.start()
        assert in_factory.wait(timeout=5), "factory never ran"

        # The entry is pinned by the in-flight build, so this cannot drop it.
        mgr.close(sess.conversation_id)
        release.set()
        builder.join(timeout=10)

        # Exactly one socket was ever opened for this conversation...
        assert len(factory.calls) == 1
        # ...it was not cached, and it was closed rather than leaked.
        assert sess.conversation_id not in mgr._entries
        assert factory.channels[0].alive is False
        assert outcome and isinstance(outcome[0], ProviderChannelClosed)
    finally:
        release.set()
        mgr.stop_reaper()
