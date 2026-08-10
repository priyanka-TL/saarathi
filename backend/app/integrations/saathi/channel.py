"""One conversation's Saathi WebSocket.

Responsible for: opening the socket with a real ELEVATE token and running turns.
Used by: the shared channel pool, one channel per conversation.

DELIBERATELY A THIN SUBCLASS OF MitraChannel. Saathi runs the same Django
consumer (`ws/common/` -> AsyncSocketConsumer), so the handshake frame, the
turn frame, the settle window, the echo discard, the idle-gap flush and the
frame vocabulary are all identical -- verified against live traffic. The ONE
difference is that `access_token` must be a real JWT, and MitraChannel already
takes it as a parameter.

Re-implementing the turn loop here would duplicate ~150 lines of hard-won
behaviour (§1.2 echo discard, §1.3 accumulate-until-finish_reason, the
idle-gap rule) so that it could drift from the Mitra copy that a shared Django
release would change on both sides at once.
"""
from __future__ import annotations

from typing import Callable, Optional

import websocket

from app.core.logger import get_logger
from app.integrations.mitra.ws_channel import MitraChannel
from app.integrations.saathi.auth import TokenProvider

logger = get_logger(__name__)


class SaathiChannel(MitraChannel):
    """A Mitra-protocol channel authenticated as a real ELEVATE user.

    :param tokens: consulted once, at connect time. A token rejected mid-session
        closes the socket; the pool then reconnects, and that reconnect mints a
        fresh token because `get()` is called again here.
    """

    def __init__(
        self,
        spec,
        sess,
        conn,
        tokens: TokenProvider,
        ws_factory: Callable[[], "websocket.WebSocket"] = websocket.WebSocket,
    ) -> None:
        # Resolved BEFORE connecting: minting can fail (bad credentials, ELEVATE
        # down), and failing before a socket exists leaves nothing to clean up.
        access_token = tokens.get()
        super().__init__(spec, sess, conn, ws_factory=ws_factory, access_token=access_token)
