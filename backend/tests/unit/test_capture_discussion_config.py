"""Pins the settings that matter in src/config/agents/capture_discussion.yaml.

The sibling file test_record_stories_config.py explains why this is necessary:
RemoteSpec, RoutingSpec, LimitsSpec and FeaturesSpec do NOT set
extra="forbid" (only BaseAgentSpec does -- verified in src/domain/agent_spec.py),
so a typo'd field name inside `remote:`, `routing:` or `limits:` is silently
dropped rather than rejected at config-sync time. Until now this agent had only
two of its fields asserted anywhere, while its sibling had ten.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from app.domain.agent_spec import AgentSpec, RemoteFlowAgentSpec

YAML_PATH = Path(__file__).parents[2] / "app" / "config" / "agents" / "capture_discussion.yaml"

_adapter = TypeAdapter(AgentSpec)


def _load_spec() -> RemoteFlowAgentSpec:
    raw = yaml.safe_load(YAML_PATH.read_text())
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


def test_remote_env_var_names():
    """These three are how the interview lands on the SAME company bot the
    Mitra portal's chaupal socket uses: both consumers resolve it as
    CompanyBot.objects.get(company=profile.company, route=bot_route), so
    bot_route_env (-> /shikshalokam_chaupal) and company_env (-> the company
    slug) together pick the bot, and flow_name selects the story branch at
    finalisation. Nothing else in Mitra's story or PDF path distinguishes
    Saarthi's socket from the portal's."""
    remote = _load_spec().remote
    assert remote.provider == "mitra"
    assert remote.flow_name == "guest-discussion"
    # NOT MITRA_STORY_BOT_ROUTE. These two agents differ by exactly one env var
    # and one flow name, and swapping either sends the interview to the wrong
    # Mitra bot with no error -- just the wrong questions.
    assert remote.bot_route_env == "MITRA_DISCUSSION_BOT_ROUTE"
    assert remote.company_env == "MITRA_COMPANY"


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
    raw_sibling = yaml.safe_load((YAML_PATH.parent / "record_stories.yaml").read_text())
    sibling = _adapter.validate_python(raw_sibling)
    assert _load_spec().routing.priority < sibling.routing.priority


def test_default_is_false_so_general_support_remains_the_sole_default():
    assert _load_spec().default is False


def test_router_and_direct_selectable():
    routing = _load_spec().routing
    assert routing.router_selectable is True
    assert routing.direct_selectable is True
