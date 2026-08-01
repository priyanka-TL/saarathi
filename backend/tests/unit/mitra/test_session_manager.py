"""Unit tests for MitraSessionManager against a fake channel factory.

No real sockets. Reap intervals/idle thresholds are kept small (tens of
milliseconds) so the suite stays fast while still exercising real
wall-clock timing logic for the idle reaper.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import List, Tuple

from app.integrations.mitra.session_manager import MitraSessionManager


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


@dataclass
class _Settings:
    mitra_max_open_channels: int = 200
    mitra_idle_close_s: float = 1200.0


class _FakeChannel:
    def __init__(self, spec, sess, settings):
        self.spec = spec
        self.sess = sess
        self.settings = settings
        self.alive = True
        self.close_calls: List[bool] = []

    def close(self, quiet: bool = False) -> None:
        self.alive = False
        self.close_calls.append(quiet)


class _FakeChannelFactory:
    def __init__(self):
        self.calls: List[Tuple] = []
        self.channels: List[_FakeChannel] = []

    def __call__(self, spec, sess, settings) -> _FakeChannel:
        self.calls.append((spec, sess, settings))
        ch = _FakeChannel(spec, sess, settings)
        self.channels.append(ch)
        return ch


def _new_sess(remote_session_id: str = "remote-1") -> _Session:
    return _Session(conversation_id=uuid.uuid4(), remote_session_id=remote_session_id)


# ---------------------------------------------------------------------------
# Acceptance 1: a miss re-authenticates with the stored session id and resumes
# ---------------------------------------------------------------------------


def test_miss_reauthenticates_with_stored_session_id_and_resumes():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess = _new_sess(remote_session_id="remote-abc")

        ch1 = mgr.acquire(spec, sess)
        assert len(factory.calls) == 1
        assert factory.calls[0][1].remote_session_id == "remote-abc"

        # Simulate a connection loss detected elsewhere.
        ch1.alive = False

        # Caller re-reads the session from Postgres (same remote_session_id
        # here, since Mitra's own DB -- not the socket -- holds the real
        # interview state) and calls acquire() again.
        ch2 = mgr.acquire(spec, sess)
        assert len(factory.calls) == 2
        assert factory.calls[1][1].remote_session_id == "remote-abc"
        assert ch2 is not ch1
    finally:
        mgr.stop_reaper()


def test_hit_returns_same_channel_without_reauthenticating():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = mgr.acquire(spec, sess)
        ch2 = mgr.acquire(spec, sess)
        assert ch1 is ch2
        assert len(factory.calls) == 1
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Acceptance 2: LRU eviction respects the bound; evicted channels are closed
# ---------------------------------------------------------------------------


def test_lru_eviction_respects_bound_and_closes_evicted():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(mitra_max_open_channels=2), channel_factory=factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()

        ch1 = mgr.acquire(spec, sess1)
        ch2 = mgr.acquire(spec, sess2)
        ch3 = mgr.acquire(spec, sess3)  # bound is 2 -> evicts ch1 (least recently used)

        assert len(mgr._chans) == 2
        assert ch1.alive is False
        assert ch1.close_calls == [True]

        # ch2/ch3 remain hits -- no new factory calls.
        assert mgr.acquire(spec, sess2) is ch2
        assert mgr.acquire(spec, sess3) is ch3
        assert len(factory.calls) == 3
    finally:
        mgr.stop_reaper()


def test_lru_touch_on_hit_changes_eviction_order():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(mitra_max_open_channels=2), channel_factory=factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()

        ch1 = mgr.acquire(spec, sess1)
        ch2 = mgr.acquire(spec, sess2)
        mgr.acquire(spec, sess1)  # touch sess1 -> now sess2 is least recently used

        ch3 = mgr.acquire(spec, sess3)  # should evict sess2, not sess1

        assert ch2.alive is False
        assert ch1.alive is True
        assert len(mgr._chans) == 2
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# reacquire / close / close_all
# ---------------------------------------------------------------------------


def test_reacquire_always_creates_fresh_channel_even_if_alive():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        spec, sess = _Spec(), _new_sess()
        ch1 = mgr.acquire(spec, sess)
        assert ch1.alive is True

        ch2 = mgr.reacquire(spec, sess)
        assert ch2 is not ch1
        assert ch1.close_calls == [True]
        assert len(factory.calls) == 2
        assert mgr.acquire(spec, sess) is ch2  # the fresh one is now cached
    finally:
        mgr.stop_reaper()


def test_close_removes_and_closes_only_that_conversation():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2 = _new_sess(), _new_sess()
        ch1 = mgr.acquire(spec, sess1)
        ch2 = mgr.acquire(spec, sess2)

        mgr.close(sess1.conversation_id)

        assert ch1.alive is False
        assert ch2.alive is True
        assert sess1.conversation_id not in mgr._chans
        assert sess2.conversation_id in mgr._chans
    finally:
        mgr.stop_reaper()


def test_close_on_unknown_conversation_id_is_a_no_op():
    mgr = MitraSessionManager(_Settings(), channel_factory=_FakeChannelFactory(), reap_interval_s=1000)
    try:
        mgr.close(uuid.uuid4())  # must not raise
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Acceptance 3: atexit closes all channels
# ---------------------------------------------------------------------------


def test_close_all_closes_every_channel():
    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        spec = _Spec()
        sess1, sess2, sess3 = _new_sess(), _new_sess(), _new_sess()
        chans = [mgr.acquire(spec, s) for s in (sess1, sess2, sess3)]

        mgr.close_all()

        assert all(ch.alive is False for ch in chans)
        assert all(ch.close_calls == [True] for ch in chans)
        assert len(mgr._chans) == 0
    finally:
        mgr.stop_reaper()


def test_atexit_registers_close_all(monkeypatch):
    registered = {}

    def fake_register(fn, *args, **kwargs):
        registered["fn"] = fn

    monkeypatch.setattr("app.integrations.mitra.session_manager.atexit.register", fake_register)

    factory = _FakeChannelFactory()
    mgr = MitraSessionManager(_Settings(), channel_factory=factory, reap_interval_s=1000)
    try:
        assert "fn" in registered

        spec, sess = _Spec(), _new_sess()
        ch = mgr.acquire(spec, sess)

        registered["fn"]()  # simulate process exit invoking the registered hook

        assert ch.alive is False
        assert len(mgr._chans) == 0
    finally:
        mgr.stop_reaper()


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------


def test_idle_reaper_closes_channels_idle_beyond_threshold():
    factory = _FakeChannelFactory()
    settings = _Settings(mitra_idle_close_s=0.05)
    mgr = MitraSessionManager(settings, channel_factory=factory, reap_interval_s=0.02)
    try:
        spec, sess = _Spec(), _new_sess()
        ch = mgr.acquire(spec, sess)
        assert ch.alive is True

        time.sleep(0.2)

        assert ch.alive is False
        assert sess.conversation_id not in mgr._chans
    finally:
        mgr.stop_reaper()


def test_idle_reaper_cleans_up_dead_channels_before_idle_threshold():
    factory = _FakeChannelFactory()
    settings = _Settings(mitra_idle_close_s=1000.0)  # long idle threshold
    mgr = MitraSessionManager(settings, channel_factory=factory, reap_interval_s=0.02)
    try:
        spec, sess = _Spec(), _new_sess()
        ch = mgr.acquire(spec, sess)
        ch.alive = False  # died externally, well before the idle threshold

        time.sleep(0.1)

        assert sess.conversation_id not in mgr._chans
    finally:
        mgr.stop_reaper()


def test_reaper_thread_is_daemon():
    mgr = MitraSessionManager(_Settings(), channel_factory=_FakeChannelFactory(), reap_interval_s=1000)
    try:
        assert mgr._reaper.daemon is True
    finally:
        mgr.stop_reaper()
