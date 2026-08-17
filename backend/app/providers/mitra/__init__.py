"""The Mitra provider: a guest interview that finalises into a story and a PDF.

Imports `provider` for the `@register_provider` side effect, which is what the
registry's pkgutil walk relies on.

A PEER OF `app/providers/saathi/`, not its base. Neither package imports the
other; the protocol they share lives in `app/providers/ws_flow/` and
`app/providers/transport/`, and an import-linter contract enforces it.
"""
from app.providers.mitra import provider  # noqa: F401  (import for registration)
