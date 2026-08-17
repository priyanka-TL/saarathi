"""The most-specific-wins scope resolution rule, as one SQL fragment.

Configuration is scoped by `(tenant_id, organization_id)` and resolves in this
order:

    (tenant, org)  >  (tenant, 'default')  >  ('default', 'default')

Three tables encode that rule (`capabilities`, `agent_configs` twice, once
directly and once through a LATERAL), and before this module each spelled the
ranking out by hand. Three copies of a precedence rule is three chances for one
to invert -- and an inverted rank does not fail loudly. It silently serves one
tenant another tenant's configuration, which is exactly the failure
`tests/guards/test_tenant_isolation.py` exists to catch.

Callers pass their own table alias because the same ranking is applied to
different tables in the same query.
"""
from __future__ import annotations

#: Bound parameter names every caller must supply alongside this fragment.
SCOPE_PARAMS = ("tenant_id", "organization_id", "default_scope")


def scope_precedence_order_by(alias: str) -> str:
    """`ORDER BY` terms ranking rows of `alias` from most to least specific.

    Emits two `CASE` expressions, organization first: an exact organization
    match sorts ahead of a tenant-wide row, which sorts ahead of the default.

    ORDER MATTERS AND IS NOT ARBITRARY. Organization is the narrower scope, so
    it must be the primary key of the sort; swapping the two terms would let a
    tenant-wide row outrank an organization-specific one and hand an org the
    configuration it had specifically overridden.

    Returns a bare fragment with no leading `ORDER BY` keyword, so callers can
    place it after their own leading terms -- `capabilities` needs `key` first
    because Postgres requires a `DISTINCT ON` expression to lead the `ORDER BY`.

    :param alias: the table alias the CASE expressions qualify, e.g. ``"c"``.
    :returns: SQL text expecting the bound parameters in :data:`SCOPE_PARAMS`.
    """
    return (
        f"CASE WHEN {alias}.organization_id = :organization_id THEN 0 ELSE 1 END,\n"
        f"                 CASE WHEN {alias}.tenant_id = :tenant_id THEN 0 ELSE 1 END"
    )


def scope_candidate_filter(alias: str) -> str:
    """`WHERE` terms narrowing `alias` to the rows this scope may inherit.

    The candidate set is "my scope, or the default" on both axes; the ranking
    above then picks the winner among them.

    MUST NOT be combined with a status/visibility filter in the same query
    level. Filtering candidates before the ranking runs drops a tenant's own
    row from the running and lets the default win in its place -- so a tenant
    that disabled something would see the DEFAULT version of it rather than
    seeing it hidden. Resolve the winner first, then decide whether the winner
    is showable. See the outer query in `capability_service._CAPABILITIES_SQL`.
    """
    return (
        f"{alias}.tenant_id IN (:tenant_id, :default_scope)\n"
        f"          AND {alias}.organization_id IN (:organization_id, :default_scope)"
    )
