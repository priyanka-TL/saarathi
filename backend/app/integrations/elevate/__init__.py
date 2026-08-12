"""ELEVATE user-service integration.

Ported from Mitra's `chatbot/utils/elevate/profile_utils.py`, minus the Django
`Profile` upsert: Mitra keeps a local row keyed on the ELEVATE user id, whereas
Saarthi reads and writes ELEVATE directly and stores no copy, so there is
nothing that can drift out of date.

Only the profile surface lives here. Verifying the JWT ELEVATE issues at login
is a separate concern with no HTTP call behind it -- see app/services/identity.py.
"""
from app.integrations.elevate.client import ElevateUserClient
from app.integrations.elevate.exceptions import (
    ElevateError,
    ElevateRejected,
    ElevateTimeout,
    ElevateUnauthorized,
    ElevateUpstreamError,
)

__all__ = [
    "ElevateError",
    "ElevateRejected",
    "ElevateTimeout",
    "ElevateUnauthorized",
    "ElevateUpstreamError",
    "ElevateUserClient",
]
