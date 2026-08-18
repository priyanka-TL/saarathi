"""A bounded pool of live channels, keyed by conversation.

Responsible for: reusing one transport per conversation, and reaping idle ones.
Used by: ProviderRegistry builds one per stateful provider.

IN PROCESS MEMORY, which is why any enabled provider whose transport is stateful
pins the app to a single worker: a second worker opens a SECOND channel for the
same conversation. `RemoteProvider.stateful_transport` is what declares that, and
app/core/runtime.py enforces it at startup.

ONE POOL PER PROVIDER, NOT ONE SHARED POOL. Every pool keys on conversation_id
and a conversation can move between agents, so a shared pool would let one
provider's turn find another provider's socket under the same key and
re-authenticate it against the wrong deployment. The bounds are per pool, which
is also why they stay env-level: there is nothing per-tenant for a process-wide
ceiling to apply to.

Knows nothing about payloads. A channel need only offer `alive`, `conn_checksum`
and `close` -- see the Channel protocol below.

WHAT THE POOL LOCK DOES AND DOES NOT COVER
------------------------------------------
`self._lock` guards METADATA ONLY. Every blocking call -- building a channel
(a TCP+TLS connect, a handshake and a settle window) and closing one -- happens
with the lock RELEASED:

                ┌── conversation A → build
    pool lock ──┼── conversation B → build      builds run concurrently
                └── conversation C → build

Building under the pool lock is what this file used to do, and it serialised
every cold start process-wide: N simultaneous first turns cost N handshakes
end to end, and one unreachable remote stalled every other conversation for its
full connect timeout. `reacquire`, `close`, `close_all` and `_reap_once` already
worked this way; `acquire` is now consistent with them.

WHY ONE ENTRY PER CONVERSATION, REFCOUNTED
------------------------------------------
Building outside the pool lock needs a second, per-conversation lock so two
threads cannot build the same conversation at once. Keeping that lock in a dict
of its own -- alongside the channels and their timestamps -- is a race, because a
thread that is BUILDING is precisely one whose conversation is absent from the
channel map: it has taken the stale channel out and not yet put a new one in. Any
cleanup path keyed on that map (`close`, `close_all`, `_reap_once`) would drop
the build lock while it was held, and the next thread would mint a fresh lock and
build a duplicate socket -- silently.

So the three maps are one `_Entry` per conversation, and `_maybe_drop` holds the
single rule that closes the hole:

    an entry may be removed only when it has no channel AND no waiters

`waiters` counts the threads holding or awaiting `build_lock`, so a build in
flight pins its own entry and the lock it holds can never be replaced underneath
it. Every removal path clears `channel` and then calls `_maybe_drop`; none of
them pops the map directly.
"""
from __future__ import annotations

import atexit
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol
from uuid import UUID

from app.core.logger import get_logger
from app.providers.errors import ProviderChannelClosed

logger = get_logger("provider_channel_pool")


class Channel(Protocol):
    """The only surface the pool needs from a transport."""

    conn_checksum: str

    @property
    def alive(self) -> bool: ...

    def close(self, quiet: bool = False) -> None: ...


def _close_quietly(ch: Optional[Any]) -> None:
    """Close a channel outside the pool lock, swallowing failures.

    A socket that will not close is not a reason to fail the caller's turn, and
    the idle reaper sweeps whatever is left. `None` is accepted so callers can
    hand over a slot that turned out to be empty without branching.
    """
    if ch is None:
        return
    try:
        ch.close(quiet=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to close a provider channel: %s", e)


@dataclass
class _Entry:
    """One conversation's slot in the pool.

    :param build_lock: serialises builds for THIS conversation only. Different
        conversations hold different locks and build concurrently, which is the
        whole point of moving the factory out from under the pool lock.
    :param channel: the live channel, or None while cold, mid-build, or after a
        stale one has been taken out.
    :param waiters: threads holding or awaiting `build_lock`. Non-zero PINS the
        entry -- see `_maybe_drop`, and the module docstring for why.
    :param close_requested: an explicit `close()`/`close_all()` arrived while a
        build was in flight. The builder honours it rather than caching a
        channel the caller asked to be rid of -- see `acquire`.
    """

    build_lock: threading.Lock = field(default_factory=threading.Lock)
    channel: Any = None
    last_used: float = 0.0
    waiters: int = 0
    close_requested: bool = False


class ChannelPool:
    """LRU pool with an idle reaper and an atexit sweep.

    :param max_open: hard ceiling; the least recently used is evicted at it.
    :param idle_close_s: a channel untouched for this long is reaped.

    THE FACTORY IS PASSED PER CALL, not held on the pool. A pool is process-wide
    per provider, but the thing that builds a channel is bound to one resolved
    CONNECTION -- its credentials, its endpoint, its token provider. Holding a
    single factory here would have meant either one pool per connection (losing
    the process-wide ceiling the bounds exist to enforce) or a factory that
    ignores the caller's scope.
    """

    def __init__(
        self,
        *,
        max_open: int,
        idle_close_s: float,
        name: str = "provider",
        reap_interval_s: float = 60.0,
    ) -> None:
        self._max = max_open
        self._idle_close_s = idle_close_s
        self._name = name

        self._lock = threading.RLock()
        self._entries: "OrderedDict[UUID, _Entry]" = OrderedDict()

        self._stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_loop, args=(reap_interval_s,),
            daemon=True, name=f"{name}-channel-reaper",
        )
        self._reaper.start()

        atexit.register(self.close_all)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, sess, conn, factory: Callable[[], Any]) -> Any:
        """Hit: move to end, return. Miss (restart / eviction / cold start):
        build a fresh channel with whatever remote_session_id `sess` carries --
        correct because the conversation's state lives in the provider's own
        database, not in the connection.

        A STALE CONNECTION COUNTS AS A MISS. `conn` is the configuration this
        scope resolves to RIGHT NOW; a pooled channel opened against a different
        one is talking to the wrong deployment, or as the wrong company, with the
        wrong Origin. Comparing checksums here is what makes a runtime config
        change take effect on the next turn instead of whenever the idle reaper
        happens to get to it.

        THREE PHASES, AND THE MIDDLE ONE HOLDS NO POOL LOCK. Phase 1 claims the
        slot under `self._lock`; phase 2 does every blocking thing (closing the
        stale channel and the eviction victims, then `factory()`) holding only
        this conversation's `build_lock`; phase 3 files the result. Taking
        `build_lock` while `self._lock` was still held would re-serialise the
        pool as effectively as building under it did -- and would read as
        correct, which is why the acquisition order is spelled out here.
        """
        conv_id = sess.conversation_id

        # -- phase 1: metadata only, under the pool lock --------------------
        with self._lock:
            entry = self._entries.get(conv_id)
            if entry is None:
                entry = _Entry()
                self._entries[conv_id] = entry
            else:
                cached = entry.channel
                if (
                    cached is not None
                    and cached.alive
                    and cached.conn_checksum == conn.checksum
                ):
                    self._entries.move_to_end(conv_id)
                    entry.last_used = time.monotonic()
                    return cached

            # Whatever was here is stale; take it out so no other thread can
            # hand it back, and close it in phase 2.
            stale = entry.channel
            entry.channel = None
            entry.waiters += 1
            self._entries.move_to_end(conv_id)
            # Cannot evict this entry now: waiters is already non-zero.
            victims = self._evict_if_needed()
            build_lock = entry.build_lock

        # -- phase 2: no pool lock; everything that can block lives here ----
        try:
            _close_quietly(stale)
            for victim in victims:
                _close_quietly(victim)

            with build_lock:
                # Another thread may have built this conversation while we
                # queued on build_lock. Re-check before paying for a socket.
                with self._lock:
                    cached = entry.channel
                    if (
                        cached is not None
                        and cached.alive
                        and cached.conn_checksum == conn.checksum
                    ):
                        self._entries.move_to_end(conv_id)
                        entry.last_used = time.monotonic()
                        return cached

                new_ch = factory()

                # -- phase 3: file the result, metadata only ---------------
                with self._lock:
                    discard = entry.close_requested
                    if not discard:
                        entry.channel = new_ch
                        entry.last_used = time.monotonic()
                        self._entries.move_to_end(conv_id)

                if discard:
                    # close() landed mid-build. Before the factory moved out of
                    # the pool lock this could not happen -- close() simply
                    # waited -- so honouring it here is what keeps the old
                    # behaviour: a closed conversation does not come back with a
                    # live socket. The caller sees the same error a channel that
                    # died under it raises, and BaseWsFlowProvider.turn already
                    # has exactly one re-establishment attempt for that.
                    _close_quietly(new_ch)
                    raise ProviderChannelClosed(
                        "conversation was closed while its channel was being opened"
                    )
                return new_ch
        finally:
            with self._lock:
                entry.waiters -= 1
                self._maybe_drop(conv_id, entry)

    def reacquire(self, sess, conn, factory: Callable[[], Any]) -> Any:
        """Force a fresh channel after a connection loss. Always discards and
        reconnects, even if the cached channel still (incorrectly) reports
        alive -- that is the whole point of a caller explicitly telling the pool
        not to trust it."""
        conv_id = sess.conversation_id
        with self._lock:
            entry = self._entries.get(conv_id)
            old = None
            if entry is not None:
                old = entry.channel
                entry.channel = None
                self._maybe_drop(conv_id, entry)
        _close_quietly(old)
        return self.acquire(sess, conn, factory)

    def close(self, conversation_id: UUID) -> None:
        with self._lock:
            entry = self._entries.get(conversation_id)
            if entry is None:
                return
            ch = entry.channel
            entry.channel = None
            if entry.waiters:
                # A build is in flight; it must not cache its result.
                entry.close_requested = True
            self._maybe_drop(conversation_id, entry)
        _close_quietly(ch)

    def close_all(self) -> None:
        with self._lock:
            chans = []
            for cid, entry in list(self._entries.items()):
                if entry.channel is not None:
                    chans.append(entry.channel)
                    entry.channel = None
                if entry.waiters:
                    entry.close_requested = True
                self._maybe_drop(cid, entry)
        for ch in chans:
            _close_quietly(ch)

    # ------------------------------------------------------------------
    # Entry lifecycle
    # ------------------------------------------------------------------

    def _maybe_drop(self, conv_id: UUID, entry: _Entry) -> None:
        """Caller already holds self._lock.

        THE ONE RULE THAT MAKES THE BUILD LOCK SAFE: an entry leaves the map only
        when it holds no channel and no thread is building through it. Dropping a
        pinned entry would let the next caller create a second `build_lock` for
        the same conversation and open a duplicate socket, with nothing raised
        and nothing logged. Every removal path goes through here; none pops
        `_entries` directly.
        """
        if entry.channel is None and entry.waiters == 0:
            self._entries.pop(conv_id, None)

    # ------------------------------------------------------------------
    # Eviction / reaping
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> List[Any]:
        """Caller already holds self._lock. RETURNS the victims; the caller
        closes them after releasing the lock.

        Only entries that actually hold a channel count towards `max_open` --
        one mid-build is not yet a socket -- and an entry with waiters is never
        evicted, since closing a channel another thread is about to publish
        would leak it. If everything live is pinned, the pool runs briefly over
        its ceiling rather than blocking; concurrent builds bound the overshoot.
        """
        victims: List[Any] = []
        while True:
            live = sum(1 for e in self._entries.values() if e.channel is not None)
            if live < self._max:
                break
            # OrderedDict order is LRU: entries are moved to the end on every
            # hit, so the first evictable one is the least recently used.
            victim_id = next(
                (
                    cid
                    for cid, e in self._entries.items()
                    if e.channel is not None and e.waiters == 0
                ),
                None,
            )
            if victim_id is None:
                break
            entry = self._entries[victim_id]
            victims.append(entry.channel)
            entry.channel = None
            self._maybe_drop(victim_id, entry)
        return victims

    def _reap_loop(self, interval_s: float) -> None:
        while not self._stop.wait(timeout=interval_s):
            try:
                self._reap_once()
            except Exception as e:  # noqa: BLE001
                logger.error("idle reaper failed: %s", e, exc_info=True)

    def _reap_once(self) -> None:
        """Closes channels idle beyond idle_close_s (or no longer alive) and
        records NOTHING in the database: the agent_sessions row stays
        awaiting_user and re-authenticates on the next acquire().

        Deliberately does NOT set `close_requested`: reaping means "this socket
        went cold", not "forget this conversation", so a build that is in flight
        is left to finish and publish normally.
        """
        now = time.monotonic()
        with self._lock:
            removed = []
            for cid, entry in list(self._entries.items()):
                ch = entry.channel
                if ch is None:
                    # Cold or mid-build: drop it only if nothing is using it.
                    self._maybe_drop(cid, entry)
                    continue
                if not ch.alive or now - entry.last_used >= self._idle_close_s:
                    removed.append(ch)
                    entry.channel = None
                    self._maybe_drop(cid, entry)
        for ch in removed:
            _close_quietly(ch)

    def stop_reaper(self) -> None:
        """Test/shutdown helper: stop the reaper thread without closing channels."""
        self._stop.set()
