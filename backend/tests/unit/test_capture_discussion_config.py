"""Pins the settings that matter in the seeded `capture_discussion` config.

These used to read app/config/agents/capture_discussion.yaml. There is no YAML any
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
#: envelope; 0019 moves this agent onto its own bot and opts it in to sending
#: the caller's profile; 0020 takes that opt-in back out. What the database
#: actually holds -- and therefore what the application validates -- is all four
#: applied in sequence, so that is what these assertions read. Composing them
#: beats restating the end state: a migration whose edit changes and a pin that
#: does not would otherwise agree with each other and disagree with the
#: database.
_GENERALIZE = _VERSIONS / "0013_generalize_remote_providers.py"
_DISCUSSION_BOT = _VERSIONS / "0019_discussion_bot_route.py"
_DROPS_USER_PROFILE = _VERSIONS / "0020_discussion_drops_user_profile.py"

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
    discussion_bot = _load_migration("_discussion_bot_0019", _DISCUSSION_BOT)
    drops_profile = _load_migration("_drops_user_profile_0020", _DROPS_USER_PROFILE)

    agents = {}
    for agent in seed.seed_agents():
        agent = copy.deepcopy(agent)
        if isinstance(agent.get("remote"), dict):
            agent["agent_type"] = "remote_flow"
            agent["remote"] = generalize._to_envelope(agent["remote"])
        # 0019 touches exactly one agent, and the guard below is the migration's
        # own predicate: it will not convert a row whose bot_route has drifted.
        if (
            agent["key"] == discussion_bot.AGENT_KEY
            and agent.get("remote", {}).get("options", {}).get("bot_route")
            == discussion_bot.OLD_BOT_ROUTE
        ):
            agent = discussion_bot.apply(agent)
        # 0020's own predicate: only a config that opted IN gets opted back out.
        if (
            agent["key"] == drops_profile.AGENT_KEY
            and agent.get("remote", {}).get("options", {}).get("send_user_profile")
            is True
        ):
            agent = drops_profile.apply(agent)
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
    """These three are how the interview lands on the right Mitra bot: the
    consumer resolves it as
    CompanyBot.objects.get(company=profile.company, route=bot_route), so
    bot_route and company (-> the company slug) together pick the bot, while
    flow_name selects the story branch at finalisation.

    THE ROUTE AND THE FLOW MOVED SEPARATELY, AND ONLY ONE OF THEM MOVED.
    Migration 0019 took this agent off /shikshalokam_chaupal -- a bot shared
    with the Mitra web portal and the WhatsApp service -- and onto its own.
    `flow_name` deliberately did NOT follow: Mitra's v1 /api/end-story/ branches
    on flow == 'guest-discussion' to render the minutes-of-meeting report, and
    any other value falls through to a generic path that renders an empty
    template into a valid, downloadable, COMPLETELY BLANK PDF -- 200 from every
    call, nothing logged. That asymmetry is the whole reason both halves are
    pinned here rather than just the one that changed.

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
    assert raw["options"]["bot_route"] == "/saarthi_discussion_flow"
    assert raw["options"]["company"] == "shikshalokamstaging"
    for value in (raw["options"]["bot_route"], raw["options"]["company"]):
        assert "${" not in value


def test_the_caller_is_NOT_named_to_mitra():
    """THE REGRESSION PIN FOR THE SKIPPED INTERVIEW. Turning this back on
    without a Mitra-side change breaks the report, and does it silently.

    The flag makes the profile upsert carry the caller's ELEVATE profile, which
    means Saarthi writes `Profile.first_name`. Mitra reads a non-empty
    first_name as "we already know this person" and starts the interview at the
    CHALLENGES step instead of step 1
    (chatbot/consumers/async_consumer.py::create_chat_session).

    Steps 1-5 are what populate `story.other_params`, and other_params is where
    mom_report.py::get_user_details reads EVERYTHING except the author --
    location, organization, participants_count, discussion_date, district,
    village, pri_member, school_representative. Observed live: a discussion
    opened at current_step=6 with the autostart message recorded as the
    CHALLENGES answer, and the report lost all of those sections. The flag
    bought a reliable author line and cost four sections of the MOM report.

    So it stays false until Mitra either exempts this bot from the skip or
    falls back to the Profile for those fields. `record_stories` never opted in
    at all -- see tests/unit/providers/test_mitra_profile_context.py, which
    pins both the opted-in and opted-out wire bodies so the dormant code path
    stays covered.
    """
    assert _raw()["remote"]["options"]["send_user_profile"] is False


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
    WsChannel._authenticate already sends `access_token: None` on the
    socket -- finalising as an authenticated user contradicted both.

    Do not move either half back without the other, and not at all without a
    Mitra-side PDFTemplates row for guest-discussion plus a downloaded,
    text-checked PDF (scripts/verify_discussion_report.py) to prove it.
    """
    remote = _load_spec().remote
    assert remote.options["finalize_path"] == "/api/end-story/"
    assert remote.options["finalize_as_guest"] is True


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
