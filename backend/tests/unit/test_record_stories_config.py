"""Pins the settings that matter in src/config/agents/record_stories.yaml
(design doc §11.2). RemoteSpec and its nested models don't set
extra="forbid" (only BaseAgentSpec does -- verified in src/domain/agent_spec.py),
so a typo'd field name inside `remote:`/`routing:` would be silently ignored
rather than caught at config-sync time. These assertions are what catch that
instead.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from app.domain.agent_spec import AgentSpec, RemoteFlowAgentSpec

YAML_PATH = Path(__file__).parents[2] / "app" / "config" / "agents" / "record_stories.yaml"

_adapter = TypeAdapter(AgentSpec)


def _load_spec() -> RemoteFlowAgentSpec:
    raw = yaml.safe_load(YAML_PATH.read_text())
    spec = _adapter.validate_python(raw)
    assert isinstance(spec, RemoteFlowAgentSpec)
    return spec


def test_key_and_agent_type():
    spec = _load_spec()
    assert spec.key == "record_stories"
    assert spec.agent_type == "remote_flow"


def test_pin_session_is_true():
    """Without this the interview dies on turn 2 -- SessionService.open_for
    only creates a session row for a pin_session agent."""
    assert _load_spec().routing.pin_session is True


def test_exit_keywords_are_present():
    """Mandatory when pinning, or a user has no way out of the interview."""
    assert len(_load_spec().routing.exit_keywords) > 0


def test_priority_is_90_for_deterministic_pre_route():
    assert _load_spec().routing.priority == 90


def test_memory_strategy_is_none_not_recent():
    """Not an oversight -- Mitra reconstructs interview state server-side;
    sending history would corrupt it."""
    assert _load_spec().memory.strategy == "none"


def test_emit_options_feature_is_enabled():
    assert _load_spec().features.emit_options is True


def test_remote_env_var_names():
    remote = _load_spec().remote
    assert remote.bot_route_env == "MITRA_STORY_BOT_ROUTE"
    assert remote.company_env == "MITRA_COMPANY"
    assert remote.provider == "mitra"
    assert remote.flow_name == "guest-mi-story"


def test_finalize_path_is_v1_because_mitra_has_no_flow_row_for_this_flow():
    """THE regression pin for the end-story HTTP 500.

    /api/end-story/v2/ resolves the story bot with
    Flow.objects.get(flow_route=flow). Mitra has no Flow row for
    'guest-mi-story' (only 'guest-discussion' is registered), and its
    end_story_v2 view reports that missing row as a 500, not a 404 -- so
    every finalisation of this agent failed deterministically. v1 resolves
    the same flow from the SessionFlowName enum instead and needs no Flow
    row.

    If someone "modernises" this back to v2, the story flow breaks again in
    exactly the way that took several debugging rounds to find. Flip it only
    together with a Mitra-side Flow row for 'guest-mi-story'.
    """
    assert _load_spec().remote.finalize_path == "/api/end-story/"


def test_the_two_agents_finalize_differently_per_agent():
    """Finalisation settings are per-agent config, not a global switch.

    Both agents land on v1 now, but for unrelated reasons -- this one because
    Mitra has no Flow row for 'guest-mi-story' (v2 would 500), the sibling
    because v2 has no chaupal branch and renders its PDF through
    get_html_from_template, which returns "" and produces a blank file. Same
    endpoint, different evidence; neither reason licenses changing the other
    agent.

    finalize_as_guest is where they still diverge, and it is the part most
    likely to get "tidied" into one value: this agent sends its token in the v1
    body, the discussion agent sends `access_token: null` because Mitra picks
    the PDF template's user_type from token presence. Keep them apart.
    """
    raw = yaml.safe_load(
        (YAML_PATH.parent / "capture_discussion.yaml").read_text()
    )
    sibling = _adapter.validate_python(raw)

    assert sibling.remote.flow_name == "guest-discussion"
    assert sibling.remote.finalize_path == "/api/end-story/"
    assert sibling.remote.finalize_as_guest is True

    assert _load_spec().remote.finalize_as_guest is False


def test_router_and_direct_selectable():
    routing = _load_spec().routing
    assert routing.router_selectable is True
    assert routing.direct_selectable is True


def test_default_is_false_so_general_support_remains_the_sole_default():
    assert _load_spec().default is False
