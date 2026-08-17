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
"""
from __future__ import annotations

import atexit
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Protocol
from uuid import UUID

from app.core.logger import get_logger

logger = get_logger("provider_channel_pool")


class Channel(Protocol):
    """The only surface the pool needs from a transport."""

    conn_checksum: str

    @property
    def alive(self) -> bool: ...

    def close(self, quiet: bool = False) -> None: ...


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
        self._chans: "OrderedDict[UUID, Any]" = OrderedDict()
        self._last_used: Dict[UUID, float] = {}

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
        happens to get to it."""
        with self._lock:
            conv_id = sess.conversation_id
            ch = self._chans.get(conv_id)
            if ch is not None and ch.alive and ch.conn_checksum == conn.checksum:
                self._chans.move_to_end(conv_id)
                self._last_used[conv_id] = time.monotonic()
                return ch

            if ch is not None:
                self._chans.pop(conv_id, None)
                self._last_used.pop(conv_id, None)
                ch.close(quiet=True)

            self._evict_if_needed()

            new_ch = factory()
            self._chans[conv_id] = new_ch
            self._last_used[conv_id] = time.monotonic()
            return new_ch

    def reacquire(self, sess, conn, factory: Callable[[], Any]) -> Any:
        """Force a fresh channel after a connection loss. Always discards and
        reconnects, even if the cached channel still (incorrectly) reports
        alive -- that is the whole point of a caller explicitly telling the pool
        not to trust it."""
        with self._lock:
            old = self._chans.pop(sess.conversation_id, None)
            self._last_used.pop(sess.conversation_id, None)
        if old is not None:
            old.close(quiet=True)
        return self.acquire(sess, conn, factory)

    def close(self, conversation_id: UUID) -> None:
        with self._lock:
            ch = self._chans.pop(conversation_id, None)
            self._last_used.pop(conversation_id, None)
        if ch is not None:
            ch.close(quiet=True)

    def close_all(self) -> None:
        with self._lock:
            chans = list(self._chans.values())
            self._chans.clear()
            self._last_used.clear()
        for ch in chans:
            ch.close(quiet=True)

    # ------------------------------------------------------------------
    # Eviction / reaping
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Caller already holds self._lock. OrderedDict front == least recently
        used (moved to end on every hit)."""
        while len(self._chans) >= self._max:
            oldest_id, oldest_ch = next(iter(self._chans.items()))
            self._chans.pop(oldest_id)
            self._last_used.pop(oldest_id, None)
            oldest_ch.close(quiet=True)

    def _reap_loop(self, interval_s: float) -> None:
        while not self._stop.wait(timeout=interval_s):
            try:
                self._reap_once()
            except Exception as e:  # noqa: BLE001
                logger.error("idle reaper failed: %s", e, exc_info=True)

    def _reap_once(self) -> None:
        """Closes channels idle beyond idle_close_s (or no longer alive) and
        records NOTHING in the database: the agent_sessions row stays
        awaiting_user and re-authenticates on the next acquire()."""
        now = time.monotonic()
        with self._lock:
            stale_ids = [
                cid for cid, ch in self._chans.items()
                if not ch.alive or now - self._last_used.get(cid, now) >= self._idle_close_s
            ]
            removed = []
            for cid in stale_ids:
                removed.append(self._chans.pop(cid))
                self._last_used.pop(cid, None)
        for ch in removed:
            ch.close(quiet=True)

    def stop_reaper(self) -> None:
        """Test/shutdown helper: stop the reaper thread without closing channels."""
        self._stop.set()
