"""Remote conversation providers.

    protocol.py   the contract every provider implements
    registry.py   discovery (pkgutil + decorator), enablement, instance caching
    errors.py     the one exception hierarchy the core catches
    recovery.py   reconcile-don't-resend, vendor-free
    connection.py RemoteSpec + environment -> a frozen, checksummed connection

    transport/    reusable primitives, named for the protocol family
    ws_flow/      the base for any JSON-framed WebSocket conversational API

    mitra/        @register_provider("mitra")
    saathi/       @register_provider("saathi")

THE TWO PLATFORM PACKAGES ARE PEERS. Neither imports the other, and an
import-linter contract enforces it. They happen to run the same Django
application today, which is a fact about the PROTOCOL they speak, not about
either platform -- so the code they share lives in `transport/` and `ws_flow/`
below them, not in a package named after one of them.

Adding a platform: drop in `app/providers/<name>/` with a `@register_provider`
class and an `options_model` carrying `extra="forbid"`, then add `<name>` to
PROVIDERS_ENABLED. There is no registry file to edit and no domain file to
touch.
"""
