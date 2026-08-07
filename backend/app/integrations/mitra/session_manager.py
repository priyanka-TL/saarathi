"""The pool of live Mitra channels, keyed by conversation.

Responsible for: reusing one socket per conversation, and reaping idle ones.
Used by: the container builds one; orchestration and the routers close through it.

IN PROCESS MEMORY, which is why MITRA_ENABLED=1 pins the app to a single worker:
a second worker opens a SECOND channel for the same interview.
"""
from __future__ import annotations

import atexit
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional
from uuid import UUID

from app.integrations.mitra.ws_channel import MitraChannel
from app.core.logger import get_logger

logger = get_logger("mitra_session_manager")


class MitraSessionManager:
    def __init__(
        self,
        settings,
        channel_factory: Callable[..., MitraChannel] = MitraChannel,
        reap_interval_s: float = 60.0,
    ):
        # `settings` supplies the POOL BOUNDS only, and deliberately stays an
        # env setting: there is one pool per process, so a per-tenant channel
        # ceiling or idle timeout would have nothing to apply to. The per-scope
        # endpoint configuration arrives with each acquire() instead.
        self._channel_factory = channel_factory
        self._max = settings.mitra_max_open_channels
        self._idle_close_s = settings.mitra_idle_close_s

        self._lock = threading.RLock()
        self._chans: "OrderedDict[UUID, MitraChannel]" = OrderedDict()
        self._last_used: Dict[UUID, float] = {}

        self._stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_loop, args=(reap_interval_s,),
            daemon=True, name="mitra-session-reaper",
        )
        self._reaper.start()

        atexit.register(self.close_all)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, spec, sess, conn) -> MitraChannel:
        """Hit: move to end, return. Miss (restart / eviction / other
        worker / cold start): re-authenticate with whatever remote_session_id
        `sess` carries -- correct because Mitra's interview state lives in
        Mitra's own database, not in the connection.

        A STALE CONNECTION COUNTS AS A MISS. `conn` is the configuration this
        scope resolves to RIGHT NOW; a pooled channel opened against a different
        one is talking to the wrong Mitra instance, or as the wrong company,
        with the wrong Origin. Comparing checksums here is what makes a runtime
        config change take effect on the next turn instead of whenever the idle
        reaper happens to get to it (up to mitra_idle_close_s later)."""
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

            new_ch = self._channel_factory(spec, sess, conn)
            self._chans[conv_id] = new_ch
            self._last_used[conv_id] = time.monotonic()
            return new_ch

    def reacquire(self, spec, sess, conn) -> MitraChannel:
        """Force a fresh channel after a connection loss. Always discards
        and reconnects, even if the cached channel still (incorrectly)
        reports alive -- that's the whole point of a caller explicitly
        telling the pool not to trust it."""
        with self._lock:
            old = self._chans.pop(sess.conversation_id, None)
            self._last_used.pop(sess.conversation_id, None)
        if old is not None:
            old.close(quiet=True)
        return self.acquire(spec, sess, conn)

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
        """Caller already holds self._lock. OrderedDict front == least
        recently used (moved to end on every hit)."""
        while len(self._chans) >= self._max:
            oldest_id, oldest_ch = next(iter(self._chans.items()))
            self._chans.pop(oldest_id)
            self._last_used.pop(oldest_id, None)
            oldest_ch.close(quiet=True)

    def _reap_loop(self, interval_s: float) -> None:
        while not self._stop.wait(timeout=interval_s):
            try:
                self._reap_once()
            except Exception as e:
                logger.error("idle reaper failed: %s", e, exc_info=True)

    def _reap_once(self) -> None:
        """Closes channels idle beyond mitra_idle_close_s (or no longer
        alive) and records NOTHING in the database: the agent_sessions row
        stays awaiting_user and re-authenticates on the next acquire()."""
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
