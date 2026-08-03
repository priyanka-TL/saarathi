"""Pins the settings that matter in the seeded `capture_discussion` config.

These used to read app/config/agents/capture_discussion.yaml. There is no YAML any
more -- the catalogue is seeded by migration 0010 and edited through the config
API -- so the source of truth these assert against is the migration's own
SEED_AGENTS list, validated through the real adapter exactly as the application
validates a row read out of agent_configs.

RemoteSpec and its nested models don't set extra="forbid" (only BaseAgentSpec
does), so a typo'd field name inside `remote:`/`routing:` would be silently
ignored rather than rejected. These assertions are what catch that instead.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

from pydantic import TypeAdapter

from app.domain.agent_spec import AgentSpec, RemoteFlowAgentSpec

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "0010_seed_default_data.py"

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
    spec = importlib.util.spec_from_file_location("_seed_0010", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {a["key"]: a for a in module.seed_agents()}


def _raw() -> dict:
    return _seed_specs()["capture_discussion"]


def _load_spec() -> RemoteFlowAgentSpec:
    """The seeded dict, validated exactly as the application validates a row
    read out of agent_configs. No merge step: the seed writes a complete spec."""
    raw = copy.deepcopy(_raw())
    spec = _adapter.validate_python(raw)
    assert isinstance(spec, RemoteFlowAgentSpec)
    return spec


def test_key_and_agent_type():
    spec = _load_spec()
    assert spec.key == "capture_discussion"
    assert spec.agent_type == "remote_flow"


def test_pin_session_is_true():
    """Without this the interview dies on turn 2 -- SessionService.open_for
    only creates a session row for a pin_session agent."""
    assert _load_spec().routing.pin_session is True


def test_bot_route_and_company_are_stored_literals():
    """These three are how the interview lands on the SAME company bot the
    Mitra portal's chaupal socket uses: both consumers resolve it as
    CompanyBot.objects.get(company=profile.company, route=bot_route), so
    bot_route (-> /shikshalokam_chaupal) and company (-> the company slug)
    together pick the bot, and flow_name selects the story branch at
    finalisation. Nothing else in Mitra's story or PDF path distinguishes
    Saarthi's socket from the portal's.

    They are plain stored fields rather than the old `*_env` indirection, which
    is what lets a tenant-scoped agent_configs row point this agent at its own
    company.
    """
    remote = _load_spec().remote
    assert remote.provider == "mitra"
    assert remote.flow_name == "guest-discussion"

    raw = _raw()["remote"]
    # NOT the story bot route. These two agents differ by exactly one route and
    # one flow name, and swapping either sends the interview to the wrong Mitra
    # bot with no error -- just the wrong questions.
    assert raw["bot_route"] == "/shikshalokam_chaupal"
    assert raw["company"] == "shikshalokamstaging"
    for value in (raw["bot_route"], raw["company"]):
        assert "${" not in value


def test_finalize_is_v1_and_tokenless_because_only_that_renders_the_mom_report():
    """THE regression pin for the empty Capture Discussion PDF.

    This agent used to sit on v2 (which does work -- 'guest-discussion' IS
    registered in Mitra's Flow table, so the bot resolves and the call returns
    200 with a story id). The endpoint choice is not about bot resolution here,
    it is about which PDF renderer runs:

      v1  create_story_object: flow == GuestDiscussion -> save_chaupal_report
          -> save_shikshalokam_story -> get_story_html -> get_mom_report_html
          = the populated minutes-of-meeting report.
      v2  generate_story: no chaupal branch at all -> save_generic_story
          -> save_project_story -> get_html_from_template, which returns ""
          when no PDFTemplates row matches the flow, and save_project_story
          hands that "" to Gotenberg.

    Gotenberg renders empty HTML as an empty page and returns 200, so the story
    was created, a StoryMedia row existed, get-story returned 200 and the file
    downloaded fine -- completely blank, with nothing logged. Every signal the
    app could check was green.

    finalize_as_guest belongs in the same pin: Mitra sets
    `auth = access_token is not None` and picks the template's user_type from
    it, so presenting a token on this guest flow misses a GUEST-typed template
    and lands in the same empty-string branch. Mitra's own client for this flow
    posts `access_token: null` (storyPostSessionService._callEndStory), and
    MitraChannel._authenticate already sends `access_token: None` on the
    socket -- finalising as an authenticated user contradicted both.

    Do not move either half back without the other, and not at all without a
    Mitra-side PDFTemplates row for guest-discussion plus a downloaded,
    text-checked PDF (scripts/verify_discussion_report.py) to prove it.
    """
    remote = _load_spec().remote
    assert remote.finalize_path == "/api/end-story/"
    assert remote.finalize_as_guest is True


def test_memory_strategy_is_none_not_recent():
    """Not an oversight -- Mitra reconstructs interview state server-side;
    sending history would corrupt it."""
    assert _load_spec().memory.strategy == "none"


def test_emit_options_feature_is_enabled():
    assert _load_spec().features.emit_options is True


def test_exit_keywords_are_phrases_not_bare_words():
    """THE regression pin for silently destroyed interviews.

    This agent used to inherit RoutingSpec's bare defaults
    ["/exit", "cancel", "stop"]. Exit matching is exact now, but "stop" and
    "cancel" are still ordinary one-word answers to this interview's own
    questions ("Is there any other challenge you'd like to share?"). Abandoning
    a session is irreversible; missing a command is not.

    Five sessions in the live database were abandoned with error='user_exit' at
    step 1 this way -- e.g. conversation 836a4305, where "Children stopped
    coming to school after the monsoon" ended the interview and General Support
    answered instead, so the user never learned there would be no report.
    """
    exit_keywords = _load_spec().routing.exit_keywords
    assert exit_keywords, "an interview with no way out is worse than a false positive"
    for bare in ("stop", "cancel"):
        assert bare not in exit_keywords, (
            f"{bare!r} is a plausible answer to this interview's own questions"
        )
    assert "/exit" in exit_keywords


def test_limits_are_declared_so_an_interview_has_a_ceiling():
    """These are enforced now (RateLimits in services/orchestration.py). While
    the enforcement was a no-op stub, this agent declared no limits at all and
    a runaway interview had no turn cap whatsoever."""
    limits = _load_spec().limits
    assert limits.max_turns == 60
    assert limits.rate_limit_per_conversation_per_min == 20


def test_priority_is_below_record_stories():
    """Both agents match on story/discussion-ish keywords and Gate 3 takes the
    highest priority hit, so the ordering between them is load-bearing."""
    sibling_raw = copy.deepcopy(_seed_specs()["record_stories"])
    sibling = _adapter.validate_python(sibling_raw)
    assert _load_spec().routing.priority < sibling.routing.priority


def test_default_is_false_so_general_support_remains_the_sole_default():
    assert _load_spec().default is False


def test_router_and_direct_selectable():
    routing = _load_spec().routing
    assert routing.router_selectable is True
    assert routing.direct_selectable is True
