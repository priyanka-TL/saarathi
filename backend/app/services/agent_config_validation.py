"""Validation and redaction for an agent configuration.

Responsible for: typing the provider-specific half of a config, rejecting one
that would fail silently, and blanking unresolved credential placeholders.
Used by: the admin router, on write and on read.

TWO-STAGE VALIDATION, and this module is stage 2. `app.domain` is import-pure by
contract, so `RemoteSpec` can validate the ENVELOPE but must leave
`remote.options` opaque -- it cannot import a provider registry to find out what
belongs in there. This is where the provider's own model gets to say.

Both stages are strict. The envelope and every provider `options_model` set
`extra="forbid"`, which closes a hole that was live for a long time: nested spec
models inherited nothing from their parent's config, so a mistyped key inside
`remote` was dropped in silence and the field took its default. For a finalize
endpoint that meant an HTTP 200 and a downloadable, completely blank PDF,
reachable by one doubled letter.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

#: What a redacted value is replaced with. A fixed marker rather than an empty
#: string, so a reader can tell "this was removed" from "this was never set".
REDACTED = "<REDACTED>"

#: The marker of an unresolved environment reference, e.g. "${MITRA_ORIGIN_URL}".
_ENV_PLACEHOLDER = "${"


def redact_secrets(data: Any) -> Any:
    """Blank any string still carrying a `${...}` env placeholder.

    An unresolved reference NAMES a credential -- `${MITRA_ORIGIN_URL}` tells a
    reader which environment variable to go looking for -- so the whole value is
    replaced rather than the placeholder alone.

    Recurses through dicts and lists because a config is arbitrarily nested and
    a secret is as likely to sit under `remote.options` as at the top level.

    Note this is a backstop, not the mechanism: credentials are referenced by
    the NAME of an environment variable in `remote.auth`, and a name is not a
    secret. This catches a config written the old way, by hand.
    """
    if isinstance(data, dict):
        return {k: redact_secrets(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_secrets(i) for i in data]
    if isinstance(data, str) and _ENV_PLACEHOLDER in data:
        return REDACTED
    return data


def remote_config_problem(spec, providers, settings) -> Optional[Tuple[list, str]]:
    """Reject a remote_flow config that would fail SILENTLY at conversation time.

    Runs three checks, in order, each answering a question the previous one
    makes safe to ask:

      1. is `remote.provider` a name this deployment can serve?
      2. does `remote.options` type-check against that provider's own model?
      3. does the provider itself object to the resulting configuration?

    Check 3 is where the platform-specific rules live -- and where they should:
    they used to be written out here, so this module imported a vendor package
    to resolve a connection and check one platform's finalize endpoints.

    :returns: ``(path, msg)`` for the error envelope, or None when the config is
        fine. Returns rather than raises so the admin router can put it in its
        own bare envelope.
    """
    from app.providers.errors import ProviderConfigError, ProviderNotEnabled, UnknownProvider

    remote = spec.remote

    try:
        provider = providers.get(remote)
    except UnknownProvider as e:
        return (["remote", "provider"], str(e))
    except ProviderNotEnabled as e:
        # Deliberately NOT an error. A config for a provider this deployment has
        # switched off is a legitimate thing to write -- staging enables it,
        # production does not, and both read the same rows. The agent is hidden
        # at load time rather than rejected at write time.
        del e
        return None
    except ProviderConfigError as e:
        # Covers both a bad `options` block and credentials the environment
        # cannot supply. The provider's own model produced the message.
        return (["remote", "options"], str(e))

    return provider.validate_config(remote, settings)
