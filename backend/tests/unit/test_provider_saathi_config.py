"""Saathi's frame shape and its seeded configuration.

The quick-reply tests are a REGRESSION GUARD for a silent failure found only by
running the real thing against QA: Saathi returns its buttons as
`extra_content.quick_reply_chips`, a list of plain STRINGS, and the parser
recognised three shapes that did not include it. Four chips parsed to zero
options and every button vanished with nothing logged.

The frames below are verbatim captures from qa.saathi.shikshalokam.org.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domain.agent_spec import RemoteFlowAgentSpec, RemoteSpec
from app.providers.saathi.frames import quick_reply_chips
from app.providers.saathi.provider import SaathiProvider
from app.providers.ws_flow.frames import parse
from tests.provider_factories import agent_spec_dict, remote_dict

# ---------------------------------------------------------------------------
# Frames -- captured live, not hand-written
# ---------------------------------------------------------------------------

_BOT_WITH_CHIPS = json.dumps({"text": {
    "msg": "Is this affecting all grades, or particular grade levels?",
    "source": "bot",
    "finish_reason": "stop",
    "step": 1,
    "extra_content": {"quick_reply_chips": [
        "All grades equally", "Primary grades more",
        "Upper grades more", "Specific grades only",
    ]},
}})

_BOT_EMPTY_CHIPS = json.dumps({"text": {
    "msg": "Could you tell me a bit more?",
    "source": "bot", "finish_reason": "stop", "step": 1,
    "extra_content": {"quick_reply_chips": []},
}})

_USER_ECHO = json.dumps({"text": {
    "msg": "Student attendance is low after lunch", "source": "user",
}})


#: Saathi's chip shape is no longer built into the shared parser -- it is
#: this provider's own reader, declared in `option_readers`. Passing it
#: explicitly is what these tests are for; the next block pins that the
#: provider really does declare it, so production gets the same behaviour.
_READERS = (quick_reply_chips,)


def test_quick_reply_chips_become_options():
    """THE REGRESSION. Four chips in, four options out."""
    frame = parse(_BOT_WITH_CHIPS, _READERS)

    assert [o.label for o in frame.options] == [
        "All grades equally", "Primary grades more",
        "Upper grades more", "Specific grades only",
    ]


def test_a_chip_is_its_own_id_and_value():
    """Chips are bare strings with no id, and the value is what gets sent back
    as the user's next message -- so all three must be the text itself."""
    option = parse(_BOT_WITH_CHIPS, _READERS).options[0]

    assert option.id == option.label == option.value == "All grades equally"


def test_empty_chips_do_not_become_a_phantom_button():
    """Saathi sends `quick_reply_chips: []` on turns with no choices."""
    assert parse(_BOT_EMPTY_CHIPS, _READERS).options == []


def test_the_rest_of_a_saathi_frame_parses_as_mitra_expects():
    """Saathi runs the same Django application, so only the options shape
    differed -- source, finish_reason and step were already correct."""
    frame = parse(_BOT_WITH_CHIPS, _READERS)

    assert frame.source == "bot"
    assert frame.finish_reason == "stop"
    assert frame.step == 1
    assert frame.msg.startswith("Is this affecting")


def test_the_user_echo_is_still_discarded():
    assert parse(_USER_ECHO, _READERS).source == "user"


def test_mitra_option_shapes_are_untouched():
    """The chips shape is additive. Mitra never sends that key, and its own
    shapes must parse exactly as before."""
    state_machine = json.dumps({"text": {
        "msg": "pick", "source": "bot", "finish_reason": "stop",
        "extra_content": {"options": [{"id": "a", "label": "A", "value": "1"}]},
    }})
    sources = json.dumps({"text": {
        "msg": "here", "source": "bot", "finish_reason": "stop",
        "extra_content": {"sources": [{"url": "http://x", "title": "T"}]},
    }})

    assert [(o.id, o.label, o.value) for o in parse(state_machine).options] == [("a", "A", "1")]
    assert parse(sources).options[0].label == "T"



def test_the_provider_declares_its_own_chip_reader():
    """The chip shape used to live in the SHARED parser, inside the other
    platform's package -- a platform-specific quirk in a module every other
    platform reads. It is this provider's now, and production only gets the
    behaviour above because the provider declares it here."""
    assert quick_reply_chips in SaathiProvider.option_readers


def test_the_shared_parser_alone_does_not_know_the_chip_shape():
    """The other half of the same claim: without the reader, the shared shapes
    decline -- which is what makes this genuinely isolated rather than merely
    moved."""
    assert parse(_BOT_WITH_CHIPS).options == []


# ---------------------------------------------------------------------------
# The seeded spec
# ---------------------------------------------------------------------------


def test_the_saathi_spec_validates():
    spec = RemoteFlowAgentSpec(**agent_spec_dict("saathi", "saathi"))

    assert spec.agent_type == "remote_flow"
    assert spec.remote.provider == "saathi"
    assert spec.remote.produces_artifact is False


def test_both_platforms_are_now_the_same_agent_type():
    """THE COLLAPSE, pinned. A second delegated agent_type existed only because
    enablement was per type; with enablement per provider it had nothing left to
    express, and a new platform is a config value rather than a Postgres enum
    migration."""
    mitra = RemoteFlowAgentSpec(**agent_spec_dict("record_stories", "mitra"))
    saathi = RemoteFlowAgentSpec(**agent_spec_dict("saathi", "saathi"))

    assert mitra.agent_type == saathi.agent_type == "remote_flow"
    assert mitra.remote.provider != saathi.remote.provider


def test_produces_artifact_defaults_true():
    """The flag is opt-out: a config that has never heard of it keeps
    finalising exactly as before."""
    remote = RemoteSpec(**remote_dict("mitra"))
    assert remote.produces_artifact is True


def test_bot_route_may_not_be_empty():
    """An empty route does not fail loudly upstream -- it resolves the wrong
    bot, which is the wrong conversation with no error.

    Now enforced by the PROVIDER's own options model rather than by the shared
    domain schema, which is why it is checked through the registry.
    """
    from app.providers.saathi.spec import SaathiOptions

    with pytest.raises(ValidationError):
        SaathiOptions(bot_route="")


def test_an_unknown_provider_name_is_still_a_string_to_the_domain():
    """`provider` is deliberately NOT a Literal any more: enumerating platforms
    in the import-pure domain layer is exactly what made onboarding one a
    domain-layer edit. The name is validated against the REGISTRY instead, at
    config-write time and at load time."""
    remote = RemoteSpec(**{**remote_dict("mitra"), "provider": "some_future_platform"})
    assert remote.provider == "some_future_platform"


def test_a_provider_name_must_still_look_like_a_registry_key():
    with pytest.raises(ValidationError):
        RemoteSpec(**{**remote_dict("mitra"), "provider": "Not A Key"})


def test_the_agent_type_discriminator_picks_the_remote_model():
    from pydantic import TypeAdapter

    from app.domain.agent_spec import AgentSpec

    spec = TypeAdapter(AgentSpec).validate_python(agent_spec_dict("saathi", "saathi"))
    assert isinstance(spec, RemoteFlowAgentSpec)


# ---------------------------------------------------------------------------
# Which agents may give up a pinned session
# ---------------------------------------------------------------------------

def test_yielding_is_off_by_default():
    """The safety property. An agent that has never heard of the flag keeps the
    absolute pin, so adding it could not change any existing behaviour."""
    from app.domain.agent_spec import RoutingSpec

    assert RoutingSpec().yields_to_keyword is False


def test_only_the_assistant_yields_never_an_interview():
    """Read from the migrations, so this is what the DATABASE holds.

    An interview must never yield: a user answering "I want to tell my story
    about attendance" is talking TO it, and re-routing them would destroy the
    run. The assistant must, or its session -- which never completes -- pins the
    conversation for good.
    """
    import copy
    import importlib.util
    from pathlib import Path

    versions = Path(__file__).parents[2] / "migrations" / "versions"

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    seed = _load("_seed_0010", versions / "0010_seed_default_data.py")
    saathi_seed = _load("_saathi_0012", versions / "0012_seed_saathi_agent.py")

    interviews = {
        a["key"]: a for a in seed.seed_agents()
        if a.get("agent_type") in ("remote_flow", "saathi_flow")
    }
    for key, agent in interviews.items():
        assert agent.get("routing", {}).get("yields_to_keyword", False) is False, key

    # 0016 is what turns it on for the assistant; the seed itself does not.
    assistant = copy.deepcopy(saathi_seed.seed_spec())
    assert assistant.get("routing", {}).get("yields_to_keyword", False) is False

    generalize = _load("_yield_0016", versions / "0016_saathi_yields_pin.py")
    assert generalize.PROVIDER == "saathi", "only the assistant is switched on"
