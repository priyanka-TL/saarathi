"""A provider that pools sockets in process memory must not be duplicated.

These guards used to be phrased in terms of one platform's `*_ENABLED` flag, so
every new platform meant editing both of them and remembering which applied. The
hazard was never a particular platform -- it is process-local socket state, which
a provider declares for itself with `stateful_transport`. Passing NAMES makes
the guards indifferent to how many platforms exist, and lets a deployment running
only stateless providers legitimately escape the constraint.
"""

from __future__ import annotations

import pytest

from app.core.runtime import assert_single_worker, resolve_reloader
from app.providers.registry import stateful_enabled_names


class _Settings:
    def __init__(self, providers_enabled: str) -> None:
        self.providers_enabled = providers_enabled


# ---------------------------------------------------------------------------
# The reloader
# ---------------------------------------------------------------------------

def test_the_reloader_is_allowed_when_no_stateful_provider_is_enabled():
    assert resolve_reloader([]) is True


def test_the_reloader_is_refused_while_a_stateful_provider_is_enabled():
    """Its child re-execs and re-runs module-level setup, double-booting the
    socket pool."""
    assert resolve_reloader(["mitra"]) is False


def test_two_stateful_providers_are_no_different_from_one():
    assert resolve_reloader(["mitra", "saathi"]) is False


# ---------------------------------------------------------------------------
# The worker count
# ---------------------------------------------------------------------------

def test_a_single_worker_is_always_allowed():
    assert_single_worker(["mitra"], workers=1) is None


def test_many_workers_are_allowed_when_nothing_is_stateful():
    """THE CAPABILITY THE OLD SPELLING COULD NOT EXPRESS. A deployment running
    only stateless providers has no in-process socket pool to duplicate, so the
    single-worker constraint simply does not apply to it."""
    assert_single_worker([], workers=8) is None


def test_many_workers_are_refused_while_a_stateful_provider_is_enabled():
    with pytest.raises(RuntimeError, match="single worker"):
        assert_single_worker(["mitra"], workers=2)


def test_the_error_names_the_providers_responsible():
    """An operator has to know WHICH provider to turn off to scale out."""
    with pytest.raises(RuntimeError) as excinfo:
        assert_single_worker(["mitra", "saathi"], workers=4)

    message = str(excinfo.value)
    assert "mitra" in message and "saathi" in message
    assert "THREADPOOL_SIZE" in message


# ---------------------------------------------------------------------------
# Where the names come from
# ---------------------------------------------------------------------------

def test_the_stateful_set_is_read_off_the_providers_themselves():
    """Not off a flag named after one of them. This is what makes the two guards
    above correct for a platform nobody has written yet."""
    assert stateful_enabled_names(_Settings("mitra,saathi")) == ["mitra", "saathi"]


def test_a_disabled_provider_contributes_no_constraint():
    assert stateful_enabled_names(_Settings("mitra")) == ["mitra"]
    assert stateful_enabled_names(_Settings("")) == []


def test_an_unknown_name_is_ignored_rather_than_fatal():
    """A typo in PROVIDERS_ENABLED should hide the affected agents, not refuse
    the boot for every other one."""
    assert stateful_enabled_names(_Settings("mitra,typo_not_a_provider")) == ["mitra"]
