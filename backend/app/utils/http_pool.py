"""Connection-pool sizing for the long-lived `requests.Session`s.

Responsible for: making a shared Session's connection pool match this process's
own concurrency.
Used by: BhashiniClient, ElevateUserClient and RestTransport -- the three places
that keep one Session for the life of the process.

WHY THIS IS NOT THE DEFAULT'S PROBLEM TO SOLVE. urllib3 pools ten connections
per host. This app exceeds that in two ways at once:

  * every request runs on its own worker thread (`THREADPOOL_SIZE`, itself
    bounded by `DB_POOL_SIZE`), and
  * a single voice transcription fans out `VOICE_ASR_MAX_WORKERS` calls of its
    own, so one user can hold four Bhashini connections without any other user
    being present.

Past ten in flight urllib3 does not queue: it opens a connection outside the
pool, uses it once and discards it, logging "Connection pool is full, discarding
connection". So nothing fails -- every extra call simply pays a fresh TCP and TLS
handshake to a host the process is already connected to, which is the exact cost
a shared Session exists to avoid. It shows up as latency under concurrency and as
nothing at all when one person is testing, which is why it is worth setting
deliberately rather than discovering.

RETRIES ARE DELIBERATELY NOT CONFIGURED HERE. Each caller already owns its own
timeout budget and error mapping, and an adapter-level retry would re-send a turn
the remote platform had already recorded -- the same duplicate-submit hazard the
conversation turn lock exists to prevent.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

#: Comfortably above THREADPOOL_SIZE plus a voice fan-out on top, so the pool is
#: never what limits concurrency. Connections are opened lazily, so the headroom
#: costs an idle process nothing.
DEFAULT_POOL_MAXSIZE = 64


def size_connection_pool(
    session: requests.Session,
    *,
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
) -> requests.Session:
    """Mount an adapter on both schemes so `session` keeps `pool_maxsize`
    connections per host rather than urllib3's default ten.

    `pool_connections` (how many per-host pools are cached) is set to the same
    number: these clients each talk to one or two hosts, so it is only ever a
    ceiling, and matching the two keeps one knob instead of two.

    Returns the same session, so a constructor can wrap its own assignment.
    """
    adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
