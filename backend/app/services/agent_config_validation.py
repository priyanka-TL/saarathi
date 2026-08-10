"""Validation and redaction for an agent configuration.

Responsible for: rejecting a remote_flow config that would fail silently, and
blanking unresolved credential placeholders before a config is returned.
Used by: the admin router, on write and on read.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

#: What a redacted value is replaced with. A fixed marker rather than an empty
#: string, so a reader can tell "this was removed" from "this was never set".
REDACTED = "<REDACTED>"

#: The marker of an unresolved environment reference, e.g. "${MITRA_TOKEN}".
_ENV_PLACEHOLDER = "${"


def redact_secrets(data: Any) -> Any:
    """Blank any string still carrying a `${...}` env placeholder.

    An unresolved reference NAMES a credential -- `${MITRA_ORIGIN_URL}` tells a
    reader which environment variable to go looking for -- so the whole value is
    replaced rather than the placeholder alone.

    Recurses through dicts and lists because a config is arbitrarily nested and
    a secret is as likely to sit under `remote.connection` as at the top level.
    """
    if isinstance(data, dict):
        return {k: redact_secrets(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_secrets(i) for i in data]
    if isinstance(data, str) and _ENV_PLACEHOLDER in data:
        return REDACTED
    return data


def remote_config_problem(spec, settings) -> Optional[Tuple[list, str]]:
    """Reject a remote_flow config that would fail SILENTLY at interview time.

    THIS IS THE ONLY GATE. There is no YAML and no startup sync any more, so a
    config reaches Mitra exactly as it was written. `bot_route` and `company`
    are non-empty by schema; what the schema cannot check is `finalize_path`,
    because the endpoints are configurable and the domain layer cannot import
    Settings to express them as a Literal.

    Getting it wrong is not a loud failure: anything unrecognised falls through
    to the v1 branch and finalises with the wrong body shape, which Mitra
    ACCEPTS -- returning a story, a story_media row, a 200 from get-story and a
    downloadable, completely blank PDF, with nothing logged anywhere.

    :returns: ``(path, msg)`` for the error envelope, or None when the config
        is fine. Returns rather than raises so the admin router can put it in
        its own bare envelope.
    """
    from app.integrations.mitra.connection import resolve_connection

    remote = spec.remote

    # A flow that produces no artifact never calls finalize, so it has no
    # endpoint to validate. Checking anyway would reject Saathi, whose own
    # /api/flow-connection-info/ reports create_story: "none".
    if not getattr(remote, "produces_artifact", True):
        if remote.finalize_path:
            return (
                ["remote", "finalize_path"],
                "finalize_path must be null when produces_artifact is false -- "
                "a flow that creates no story never finalises, and naming an "
                "endpoint here would imply otherwise",
            )
        return None

    if not remote.finalize_path:
        return (
            ["remote", "finalize_path"],
            "finalize_path is required when produces_artifact is true",
        )

    # Against the endpoints THIS spec resolves to -- it may carry its own
    # remote.connection.paths, and checking against the global pair would both
    # reject correct configs and accept wrong ones.
    paths = resolve_connection(settings, remote).paths
    if not paths.is_known_finalize(remote.finalize_path):
        return (
            ["remote", "finalize_path"],
            f"finalize_path {remote.finalize_path!r} matches neither the resolved "
            f"v1 endpoint ({paths.finalize_v1!r}) nor the resolved v2 endpoint "
            f"({paths.finalize_v2!r})",
        )

    return None
