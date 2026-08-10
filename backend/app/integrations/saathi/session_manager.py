"""The pool of live Saathi channels, keyed by conversation.

Responsible for: binding the shared channel pool to SaathiChannel.
Used by: the container builds one when SAATHI_ENABLED=1.

A SEPARATE POOL FROM MITRA'S, not a shared one. Both pools key on
conversation_id, and a conversation can move between agents -- so one pool
would let a Saathi turn find a Mitra socket under the same key and
re-authenticate it against the wrong deployment. The bounds are per pool, which
is also why they stay env-level.

IN PROCESS MEMORY, so SAATHI_ENABLED=1 carries the same single-worker
requirement MITRA_ENABLED=1 does: a second worker opens a second socket for the
same conversation.
"""
from __future__ import annotations

from app.integrations.mitra.session_manager import MitraSessionManager
from app.integrations.saathi.auth import TokenProvider
from app.integrations.saathi.channel import SaathiChannel


class SaathiSessionManager(MitraSessionManager):
    """MitraSessionManager's LRU + idle reaper, bound to SaathiChannel.

    The pool is already parameterised by a channel factory and touches nothing
    Mitra-specific -- it only needs `alive`, `conn_checksum` and `close`, which
    SaathiChannel inherits. Subclassing supplies the factory and the token
    provider the factory needs; nothing else is overridden.
    """

    def __init__(self, settings, tokens: TokenProvider, **kwargs) -> None:
        super().__init__(
            settings,
            channel_factory=lambda spec, sess, conn: SaathiChannel(spec, sess, conn, tokens),
            **kwargs,
        )
