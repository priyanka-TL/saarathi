"""Reusable transport primitives, named for the protocol family they serve.

Nothing in this package knows a platform, a payload shape or an endpoint path.
That is the rule that lets a second provider speaking the same protocol reuse it
instead of subclassing a sibling provider's classes.
"""
