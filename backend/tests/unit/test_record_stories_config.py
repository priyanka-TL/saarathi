"""Pins the settings that matter in the seeded `record_stories` config.

These used to read app/config/agents/record_stories.yaml. There is no YAML any
more -- the catalogue is seeded by migration 0010 and edited through the config
API -- so the source of truth these assert against is the migration's own
SEED_AGENTS list, validated through the real adapter exactly as the application
validates a row read out of agent_configs.

Every model now sets extra="forbid", including the nested ones and the
provider's own options block -- so a typo'd field name inside `remote:` is
rejected rather than silently ignored. These assertions pin the VALUES; the
schema pins the shape.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

from pydantic import TypeAdapter

from app.domain.agent_spec import AgentSpec, RemoteFlowAgentSpec

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "0010_seed_default_data.py"
#: 0010 seeds the ORIGINAL shape; 0013 rewrites it into the provider-neutral
#: envelope; 0023 moves this agent onto its own logged-in bot. What the database
#: actually holds -- and therefore what the application validates -- is all
#: three applied in sequence, so that is what these assertions read. Composing
#: them beats restating the end state: a migration whose edit changes and a pin
#: that does not would otherwise agree with each other and disagree with the
#: database.
_GENERALIZE = _VERSIONS / "0013_generalize_remote_providers.py"
_STORY_BOT = _VERSIONS / "0023_story_bot_route.py"

_adapter = TypeAdapter(AgentSpec)


def _seed_specs() -> dict:
    """The seeded specs, loaded by path.

    Imported as a file rather than a module because `migrations/versions` is not
    a package and alembic revision filenames are not importable identifiers.

    `seed_agents()` is a FUNCTION, not a constant: the two remote_flow specs
    embed a `remote.connection` block read from the environment, so evaluating
    it at import time would freeze whatever the environment looked like then.
    What it returns is the complete spec as stored -- there is no second
    migration left to merge in.
    """
    seed = _load_migration("_seed_0010", _MIGRATION)
    generalize = _load_migration("_generalize_0013", _GENERALIZE)
    story_bot = _load_migration("_story_bot_0023", _STORY_BOT)

    agents = {}
    for agent in seed.seed_agents():
        agent = copy.deepcopy(agent)
        if isinstance(agent.get("remote"), dict):
            agent["agent_type"] = "remote_flow"
            agent["remote"] = generalize._to_envelope(agent["remote"])
        # 0023 touches exactly one agent, and the guard below is the migration's
        # own predicate: it will not convert a row whose bot_route has drifted.
        if (
            agent["key"] == story_bot.AGENT_KEY
            and agent.get("remote", {}).get("options", {}).get("bot_route")
            == story_bot.OLD_BOT_ROUTE
        ):
            agent = story_bot.apply(agent)
        agents[agent["key"]] = agent
    return agents


def _load_migration(name: str, path: Path):
    """Imported as a file rather than a module: `migrations/versions` is not a
    package and alembic revision filenames are not importable identifiers."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw() -> dict:
    return _seed_specs()["record_stories"]


def _load_spec() -> RemoteFlowAgentSpec:
    """The seeded dict, validated exactly as the application validates a row
    read out of agent_configs. No merge step: the seed writes a complete spec."""
    raw = copy.deepcopy(_raw())
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


def test_bot_route_and_company_are_stored_literals():
    """`remote.bot_route` / `remote.company` are plain stored fields, which is
    what lets a tenant-scoped agent_configs row override them.

    The route is the one 0023 moved this agent to: `/guided_guest` was a GUEST
    interview whose opening steps asked a logged-in user for their own name and
    profile. `/saarthi_story_flow` is the bot Mitra's own resolver answers with
    for the portal URL `?flow=saarthi_story_flow`, verified against QA:
    GET /api/flow-connection-info/?flow_route=saarthi_story_flow returns
    `{"bot_route": "/saarthi_story_flow"}`.

    `flow_name` is asserted UNCHANGED alongside the moved route, for the same
    reason the sibling agent's is: it selects the story renderer at
    finalisation, and moving it renders a blank PDF with a 200 from every call.

    Also asserts they carry no `${VAR}` reference: that substitution is gone,
    so one left behind would be sent to Mitra verbatim as a company slug.
    """
    raw = _raw()["remote"]
    assert raw["options"]["bot_route"] == "/saarthi_story_flow"
    assert raw["options"]["company"] == "shikshalokamstaging"
    for value in (raw["options"]["bot_route"], raw["options"]["company"]):
        assert "${" not in value

    remote = _load_spec().remote
    assert remote.provider == "mitra"
    assert remote.flow_name == "guest-mi-story"


def test_this_agent_never_names_the_caller_to_mitra():
    """THE regression pin for the sibling agent's report.

    Mitra resolves a profile by (email, company), and this agent and
    `capture_discussion` carry the SAME company and the same derived email --
    so they share ONE Profile row. Mitra reads a non-empty `first_name` as "we
    already know this person" and skips interview steps 1-5
    (chatbot/consumers/async_consumer.py, create_chat_session), and those steps
    are what populate `story.other_params`, where the minutes-of-meeting report
    reads location, organization, participants_count, discussion_date,
    district, village, pri_member and school_representative.

    So opting THIS agent in would silently re-break Capture Discussion's
    report -- the exact regression 0020 exists to undo. 0023 moves the route and
    nothing else; the profile questions are removed on the Mitra side instead,
    which costs the sibling nothing.

    Enabling this safely needs its own `company` slug first, which isolates the
    Profile row. Do not flip the flag on the shared one.
    """
    assert _raw()["remote"]["options"].get("send_user_profile") in (None, False)


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
    assert _load_spec().remote.options["finalize_path"] == "/api/end-story/"


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
    sibling_raw = copy.deepcopy(_seed_specs()["capture_discussion"])
    sibling = _adapter.validate_python(sibling_raw)

    assert sibling.remote.flow_name == "guest-discussion"
    assert sibling.remote.options["finalize_path"] == "/api/end-story/"
    assert sibling.remote.options["finalize_as_guest"] is True

    assert _load_spec().remote.options["finalize_as_guest"] is False


def test_router_and_direct_selectable():
    routing = _load_spec().routing
    assert routing.router_selectable is True
    assert routing.direct_selectable is True


def test_default_is_false_so_general_support_remains_the_sole_default():
    assert _load_spec().default is False
