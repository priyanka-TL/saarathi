"""Core identity value objects.

Responsible for: UserContext and the org membership it carries.
Used by: every layer -- this is the caller, passed down unchanged.
"""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class OrgMembership:
    org_id: str
    org_code: str
    roles: tuple[str, ...]

@dataclass(frozen=True)
class UserContext:
    user_id: str
    email: str
    display_name: str
    tenant_code: str
    orgs: tuple[OrgMembership, ...] = ()
    active_org_id: Optional[str] = None
    locale: str = "en"
    token: Optional[str] = None

    @property
    def roles(self) -> tuple[str, ...]:
        """Roles in the ACTIVE org only. Never flatten across orgs."""
        m = next((o for o in self.orgs if o.org_id == self.active_org_id), None)
        return m.roles if m else ()

@dataclass(frozen=True)
class MemorySpec:
    max_messages: int

