"""MitraSessionManager -- the channel pool: conversation_id -> MitraChannel.

In-process, LRU-bounded, with an idle reaper and atexit cleanup (design doc
§7.2, §7.4). Persistent-with-re-auth (rather than connect-per-turn) is
correct specifically because Mitra's interview state lives in MITRA'S OWN
Postgres (ChatSession.current_step, CompanyChat rows), not in the socket --
so re-authenticating with the same remote_session_id genuinely resumes the
interview rather than losing it. Connect-per-turn is disqualified because
disconnecting stamps `PAUSED` onto Mitra's own completion-tracking field
(CompanyChat.status, §1.4) and costs ~2s per turn.

This class does NOT read from Postgres itself -- `acquire(spec, sess)` takes
an already-resolved `sess` (the caller is responsible for having fetched it
fresh), and on a miss just constructs a new MitraChannel with it. That's
what makes re-authentication correct: the caller's fresh remote_session_id
flows straight into the new channel's authenticate frame.
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
        self._settings = settings
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

    def acquire(self, spec, sess) -> MitraChannel:
        """Hit: move to end, return. Miss (restart / eviction / other
        worker / cold start): re-authenticate with whatever remote_session_id
        `sess` carries -- correct because Mitra's interview state lives in
        Mitra's own database, not in the connection."""
        with self._lock:
            conv_id = sess.conversation_id
            ch = self._chans.get(conv_id)
            if ch is not None and ch.alive:
                self._chans.move_to_end(conv_id)
                self._last_used[conv_id] = time.monotonic()
                return ch

            if ch is not None:
                self._chans.pop(conv_id, None)
                self._last_used.pop(conv_id, None)
                ch.close(quiet=True)

            self._evict_if_needed()

            new_ch = self._channel_factory(spec, sess, self._settings)
            self._chans[conv_id] = new_ch
            self._last_used[conv_id] = time.monotonic()
            return new_ch

    def reacquire(self, spec, sess) -> MitraChannel:
        """Force a fresh channel after a connection loss. Always discards
        and reconnects, even if the cached channel still (incorrectly)
        reports alive -- that's the whole point of a caller explicitly
        telling the pool not to trust it."""
        with self._lock:
            old = self._chans.pop(sess.conversation_id, None)
            self._last_used.pop(sess.conversation_id, None)
        if old is not None:
            old.close(quiet=True)
        return self.acquire(spec, sess)

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
                logger.error(f"idle reaper failed: {e}")

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
