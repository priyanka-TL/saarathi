"""Every registered provider honours the contract, without being asked.

These run over the registry rather than over a hand-written list, so a provider
dropped into `app/providers/` is covered the moment it registers -- which is the
only way a rule like "every options model must be strict" survives contact with
a platform someone adds in a hurry.
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from app.providers import registry as provider_registry
from app.providers.protocol import RemoteProvider

#: The methods the core actually calls. Kept as a literal list rather than read
#: off the Protocol, so that deleting a method from the Protocol cannot silently
#: shrink what this test checks.
REQUIRED_METHODS = (
    "open_session",
    "turn",
    "is_complete",
    "finalize",
    "fetch_artifact",
    "reconcile",
    "close_channel",
    "validate_config",
)

REQUIRED_CLASSVARS = (
    "name",
    "options_model",
    "stateful_transport",
    "supports_recovery",
    "produces_artifacts",
)


def _providers():
    provider_registry.discover()
    return sorted(provider_registry._PROVIDERS.items())


def test_at_least_one_provider_is_registered() -> None:
    """Otherwise every parametrised test below passes vacuously."""
    assert _providers(), "discovery found no providers"


@pytest.mark.parametrize("name,cls", _providers())
def test_declares_every_classvar(name, cls) -> None:
    for attr in REQUIRED_CLASSVARS:
        assert hasattr(cls, attr), f"provider {name!r} does not declare `{attr}`"


@pytest.mark.parametrize("name,cls", _providers())
def test_implements_every_method(name, cls) -> None:
    for method in REQUIRED_METHODS:
        impl = getattr(cls, method, None)
        assert callable(impl), f"provider {name!r} does not implement {method}()"


@pytest.mark.parametrize("name,cls", _providers())
def test_options_model_is_strict(name, cls) -> None:
    """`extra="forbid"`, or a mistyped option is dropped in silence.

    This is the check with a real incident behind it: nested spec models
    inherited nothing from their parent's config, so renaming a field by one
    doubled letter validated cleanly, the field took its default, and the
    downstream call returned HTTP 200 with a completely blank document.
    """
    model = cls.options_model
    assert inspect.isclass(model) and issubclass(model, BaseModel), (
        f"provider {name!r}'s options_model is not a pydantic model"
    )
    assert model.model_config.get("extra") == "forbid", (
        f"provider {name!r}'s options_model {model.__name__} must set "
        'model_config = ConfigDict(extra="forbid")'
    )


@pytest.mark.parametrize("name,cls", _providers())
def test_name_matches_its_registry_key(name, cls) -> None:
    assert cls.name == name


@pytest.mark.parametrize("name,cls", _providers())
def test_satisfies_the_protocol_structurally(name, cls) -> None:
    """A Protocol is structural, so this is the closest thing to a compile-time
    check the language offers."""
    assert isinstance(RemoteProvider, type)
    for method in REQUIRED_METHODS:
        assert hasattr(RemoteProvider, method), (
            f"{method} vanished from RemoteProvider -- REQUIRED_METHODS is now "
            "checking something the contract no longer promises"
        )


def test_registering_an_untyped_provider_is_refused() -> None:
    """A provider with no options_model would leave `remote.options` unvalidated
    -- the exact hole the two-stage validation exists to close."""
    class Untyped:
        name = "untyped_test_provider"

    with pytest.raises(RuntimeError, match="options_model"):
        provider_registry.register_provider(Untyped)


def test_registering_a_lax_options_model_is_refused() -> None:
    class Lax(BaseModel):
        pass

    class LaxProvider:
        name = "lax_test_provider"
        options_model = Lax

    with pytest.raises(RuntimeError, match="extra"):
        provider_registry.register_provider(LaxProvider)


def test_duplicate_registration_is_refused() -> None:
    """Two providers answering to one key is not a configuration anyone could
    debug from its symptoms."""
    existing_name, existing_cls = _providers()[0]

    class Clashing:
        name = existing_name
        options_model = existing_cls.options_model

    with pytest.raises(RuntimeError, match="duplicate"):
        provider_registry.register_provider(Clashing)
