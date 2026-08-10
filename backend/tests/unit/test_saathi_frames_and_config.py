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

from app.domain.agent_spec import RemoteSpec, SaathiFlowAgentSpec
from app.integrations.mitra.frame_parser import parse

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


def test_quick_reply_chips_become_options():
    """THE REGRESSION. Four chips in, four options out."""
    frame = parse(_BOT_WITH_CHIPS)

    assert [o.label for o in frame.options] == [
        "All grades equally", "Primary grades more",
        "Upper grades more", "Specific grades only",
    ]


def test_a_chip_is_its_own_id_and_value():
    """Chips are bare strings with no id, and the value is what gets sent back
    as the user's next message -- so all three must be the text itself."""
    option = parse(_BOT_WITH_CHIPS).options[0]

    assert option.id == option.label == option.value == "All grades equally"


def test_empty_chips_do_not_become_a_phantom_button():
    """Saathi sends `quick_reply_chips: []` on turns with no choices."""
    assert parse(_BOT_EMPTY_CHIPS).options == []


def test_the_rest_of_a_saathi_frame_parses_as_mitra_expects():
    """Saathi runs the same Django application, so only the options shape
    differed -- source, finish_reason and step were already correct."""
    frame = parse(_BOT_WITH_CHIPS)

    assert frame.source == "bot"
    assert frame.finish_reason == "stop"
    assert frame.step == 1
    assert frame.msg.startswith("Is this affecting")


def test_the_user_echo_is_still_discarded():
    assert parse(_USER_ECHO).source == "user"


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


# ---------------------------------------------------------------------------
# The seeded spec
# ---------------------------------------------------------------------------


def _remote(**overrides):
    base = dict(
        provider="saathi", flow_name="saathi", bot_route="/saathi-bot",
        company="shikshalokamstaging",
        connection={
            "base_url": "https://qa.saathi.shikshalokam.org",
            "ws_url": "wss://qa.saathi.shikshalokam.org/ws/common/",
        },
        produces_artifact=False, finalize_path=None,
    )
    base.update(overrides)
    return base


def test_the_saathi_spec_validates():
    spec = SaathiFlowAgentSpec(
        key="saathi", name="Saathi", description="test",
        agent_type="saathi_flow", remote=_remote(),
    )

    assert spec.remote.provider == "saathi"
    assert spec.remote.produces_artifact is False
    assert spec.remote.finalize_path is None


def test_produces_artifact_defaults_true_so_mitra_is_unchanged():
    """The flag is opt-out: an existing config that has never heard of it keeps
    finalising exactly as before."""
    remote = RemoteSpec(
        provider="mitra", flow_name="guest-mi-story", bot_route="/guided_guest",
        company="c",
        connection={"base_url": "https://m.example", "ws_url": "wss://m.example/ws/"},
    )

    assert remote.produces_artifact is True
    assert remote.finalize_path == "/api/end-story/v2/"


def test_bot_route_may_not_be_empty():
    """An empty route does not fail loudly at Saathi -- it resolves the wrong
    CompanyBot, which is the wrong conversation with no error."""
    with pytest.raises(ValidationError):
        SaathiFlowAgentSpec(
            key="saathi", name="Saathi", description="t",
            agent_type="saathi_flow", remote=_remote(bot_route=""),
        )


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        SaathiFlowAgentSpec(
            key="saathi", name="Saathi", description="t",
            agent_type="saathi_flow", remote=_remote(provider="nope"),
        )


def test_the_agent_type_discriminator_picks_the_saathi_model():
    """The union is discriminated on agent_type, so a saathi_flow config must
    not validate as a remote_flow one."""
    from pydantic import TypeAdapter

    from app.domain.agent_spec import AgentSpec

    spec = TypeAdapter(AgentSpec).validate_python({
        "key": "saathi", "name": "Saathi", "description": "t",
        "agent_type": "saathi_flow", "remote": _remote(),
    })

    assert isinstance(spec, SaathiFlowAgentSpec)
