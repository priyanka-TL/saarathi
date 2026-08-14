"""Every LLM request must name a max_tokens ceiling.

THE BUG THIS EXISTS TO PREVENT, which cost a production outage on a FUNDED
account and named the wrong cause in its own error message:

`max_tokens: None` does not mean "no limit" to OpenRouter. It means "reserve
credit for the model's entire context window", so a request that will really
emit a few hundred tokens is priced at 65,536 and refused:

    code: 402, limit_source: openrouter_credits
    "You requested up to 65536 tokens, but can only afford 60029"

The message says *credits*, so the obvious reading is "top the account up" --
and topping it up does make the symptom vanish, until the balance next dips
below the cost of a full-window completion. The real defect is the unbounded
request.

It took the whole turn down rather than degrading, because BOTH LLM paths had
it: RouterService's classifier 402s, `select()` falls back to the default agent,
and that agent 402s for the same reason -- so POST /api/chat answered 502.

The two assertions below guard the two paths independently, because they are
configured in different places: the router's spec is code (it classifies, it has
no config row), the agents' is a config row.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

from app.services.router_service import RouterService

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Path 1: the router's classifier -- code, not config
# ---------------------------------------------------------------------------

def test_the_router_bounds_its_own_request():
    """It emits one small JSON verdict, so any ceiling at all is generous. What
    matters is that SOME number goes on the wire."""
    service = RouterService.__new__(RouterService)
    spec = service._router_model_spec()

    assert spec.max_tokens is not None, (
        "an unbounded router request is priced at the model's whole context "
        "window and 402s on a funded account"
    )
    assert 0 < spec.max_tokens <= 4096


def test_the_router_ceiling_is_far_below_a_context_window():
    """65,536 was what an absent ceiling implied. The point of the fix is that
    the reserved amount bears some relation to the actual reply."""
    assert RouterService.ROUTER_MAX_TOKENS < 8192


# ---------------------------------------------------------------------------
# Path 2: LLM agents -- config, set by migration 0021
# ---------------------------------------------------------------------------

def test_migration_0021_bounds_every_llm_agent():
    """Applied to the seeded general_support spec, exactly as the database
    holds it."""
    seed = _load("_seed_0010", _VERSIONS / "0010_seed_default_data.py")
    bound = _load("_bound_0021", _VERSIONS / "0021_llm_agents_bound_max_tokens.py")

    llm_specs = [a for a in seed.seed_agents() if "model" in a]
    assert llm_specs, "the seed must still ship at least one llm agent"

    for spec in llm_specs:
        # The state the defect left behind: the key is PRESENT holding null,
        # which is why 0021's predicate has to use `->>` and not `->`.
        assert spec["model"].get("max_tokens") is None

        after = bound.apply(copy.deepcopy(spec))
        assert after["model"]["max_tokens"] == bound.MAX_TOKENS
        assert after["model"]["max_tokens"] > 0


def test_0021_leaves_everything_but_the_ceiling_alone():
    """A config rewrite that changed anything else would be a silent
    re-configuration riding along with a bug fix."""
    seed = _load("_seed_0010", _VERSIONS / "0010_seed_default_data.py")
    bound = _load("_bound_0021", _VERSIONS / "0021_llm_agents_bound_max_tokens.py")

    before = next(a for a in seed.seed_agents() if "model" in a)
    after = bound.apply(copy.deepcopy(before))

    assert after["model"]["name"] == before["model"]["name"]
    assert after["model"]["temperature"] == before["model"]["temperature"]
    assert {k: v for k, v in after.items() if k != "model"} == {
        k: v for k, v in before.items() if k != "model"
    }
