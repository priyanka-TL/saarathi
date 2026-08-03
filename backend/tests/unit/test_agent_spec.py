import os
import pytest
from pydantic import ValidationError, TypeAdapter

from app.domain.agent_spec import (
    AgentSpec,
    LlmAgentSpec,
    RemoteFlowAgentSpec,
    canonical_json,
)
from app.domain.core import UserContext, OrgMembership

# Create a TypeAdapter for the discriminated union
agent_spec_adapter = TypeAdapter(AgentSpec)

def test_unknown_yaml_key_raises():
    """An unknown key must raise a ValidationError naming the field path."""
    raw_dict = {
        "agent_type": "llm",
        "key": "test_agent",
        "name": "Test Agent",
        "description": "A test agent",
        "prompt": "You are a bot",
        "model": {
            "provider": "openrouter",
            "name": "qwen",
            "temperature": 0.5,
            "timeout_s": 30.0
        },
        "typo_field": "this should crash"
    }
    
    with pytest.raises(ValidationError) as exc:
        agent_spec_adapter.validate_python(raw_dict)
        
    errors = exc.value.errors()
    # It should complain about 'typo_field' having extra fields forbidden
    assert any("typo_field" in str(e["loc"]) and e["type"] == "extra_forbidden" for e in errors)

def test_wrong_variant_fields_rejected():
    """LlmAgentSpec shouldn't accept 'remote' field and RemoteFlowAgentSpec shouldn't accept 'prompt'."""
    raw_llm = {
        "agent_type": "llm",
        "key": "test_agent",
        "name": "Test Agent",
        "description": "A test agent",
        "prompt": "You are a bot",
        "remote": {}, # wrong field for LLM
        "model": {
            "provider": "openrouter",
            "name": "qwen",
            "temperature": 0.5,
            "timeout_s": 30.0
        }
    }
    
    with pytest.raises(ValidationError) as exc:
        agent_spec_adapter.validate_python(raw_llm)
    assert any("remote" in str(e["loc"]) and e["type"] == "extra_forbidden" for e in exc.value.errors())

    raw_remote = {
        "agent_type": "remote_flow",
        "key": "test_remote",
        "name": "Test Remote",
        "description": "A test remote agent",
        "prompt": "this is for llm", # wrong field for remote
        "remote": {
            "provider": "mitra",
            "flow_name": "guest-discussion",
            "bot_route": "/test-bot-route", "company": "test-company",
            "connection": {
                "base_url": "https://mitra.example.com",
                "ws_url": "wss://mitra.example.com/ws/common/",
            },
        }
    }
    with pytest.raises(ValidationError) as exc:
        agent_spec_adapter.validate_python(raw_remote)
    assert any("prompt" in str(e["loc"]) and e["type"] == "extra_forbidden" for e in exc.value.errors())


def test_both_variants_validate():
    """Both variants should validate successfully with correct schema."""
    raw_llm = {
        "agent_type": "llm",
        "key": "test_agent",
        "name": "Test Agent",
        "description": "A test agent",
        "prompt": "You are a bot",
        "model": {
            "provider": "openrouter",
            "name": "qwen",
            "temperature": 0.5,
            "timeout_s": 30.0
        }
    }
    agent = agent_spec_adapter.validate_python(raw_llm)
    assert isinstance(agent, LlmAgentSpec)
    assert agent.model.name == "qwen"
    assert agent.model.temperature == 0.5

    raw_remote = {
        "agent_type": "remote_flow",
        "key": "test_remote",
        "name": "Test Remote",
        "description": "A test remote agent",
        "remote": {
            "provider": "mitra",
            "flow_name": "guest-discussion",
            "bot_route": "/test-bot-route", "company": "test-company",
            "connection": {
                "base_url": "https://mitra.example.com",
                "ws_url": "wss://mitra.example.com/ws/common/",
            },
        }
    }
    agent = agent_spec_adapter.validate_python(raw_remote)
    assert isinstance(agent, RemoteFlowAgentSpec)
    assert agent.remote.provider == "mitra"

def test_the_checksum_is_taken_from_the_source_dict_not_the_dumped_model():
    """A config that omits an optional field must not checksum as though it had
    set that field to the schema default.

    The checksum is HandlerFactory's cache key alongside the agent key, and
    migration 0007 recomputes it with the standard library. If it were taken
    from `model_dump()` instead, adding one optional field to the schema would
    change the checksum of every stored config at once -- invalidating every
    cached handler and making a schema change look like a configuration change
    in the audit log.
    """
    raw_dict = {
        "agent_type": "llm",
        "key": "test_checksum",
        "name": "Test Checksum",
        "description": "checksum source",
        "prompt": "prompt",
        "model": {"provider": "openrouter", "name": "qwen"},
    }

    agent = agent_spec_adapter.validate_python(raw_dict)
    json_str, sha = canonical_json(agent)

    import json as _json
    assert _json.loads(json_str) == raw_dict, "defaults must not leak into the checksum"
    # Fields the schema fills in but the caller never wrote.
    assert "routing" not in json_str
    assert "temperature" not in json_str

    import hashlib
    expected = hashlib.sha256(
        _json.dumps(raw_dict, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert sha == expected, "migration 0007 computes the digest exactly this way"


def test_a_dollar_brace_string_is_no_longer_expanded():
    """${VAR} substitution is gone with the YAML. A config row holds literal
    values, so a leftover reference must be treated as the literal string it is
    rather than silently resolving against the process environment."""
    raw_dict = {
        "agent_type": "llm",
        "key": "test_literal",
        "name": "Test Literal",
        "description": "no substitution",
        "prompt": "${SOME_VAR}",
        "model": {"provider": "openrouter", "name": "qwen"},
    }

    agent = agent_spec_adapter.validate_python(raw_dict)

    assert agent.prompt == "${SOME_VAR}"

def test_access_matching_respects_org_scoping():
    """Ensure AccessSpec.matches behaves correctly with required_roles against the active org."""
    raw_dict = {
        "agent_type": "llm",
        "key": "test_access",
        "name": "Test Access",
        "description": "Test Access rules",
        "prompt": "prompt",
        "model": {
            "name": "qwen"
        },
        "access": {
            "required_roles": ["admin"]
        }
    }
    
    agent = agent_spec_adapter.validate_python(raw_dict)
    
    # Create user with admin in org 1, but active org is org 2 (where they only have user)
    user = UserContext(
        user_id="u1",
        email="test@test.com",
        display_name="Test",
        tenant_code="t1",
        orgs=(
            OrgMembership("org1", "o1", roles=("admin",)),
            OrgMembership("org2", "o2", roles=("user",)),
        ),
        active_org_id="org2"
    )
    
    # Should NOT match because active org is org2, and roles in org2 are ("user",)
    assert agent.access.matches(user) is False
    
    # Change active org to org1 where they have "admin"
    user_switched = UserContext(
        user_id="u1",
        email="test@test.com",
        display_name="Test",
        tenant_code="t1",
        orgs=(
            OrgMembership("org1", "o1", roles=("admin",)),
            OrgMembership("org2", "o2", roles=("user",)),
        ),
        active_org_id="org1"
    )
    
    assert agent.access.matches(user_switched) is True

def test_canonical_json_is_stable():
    """Check canonical JSON checksum reproducible."""
    raw_dict = {
        "name": "Test",
        "agent_type": "llm",
        "model": {
            "name": "qwen"
        },
        "description": "desc",
        "key": "test_agent",
        "prompt": "hello",
    }
    
    agent = agent_spec_adapter.validate_python(raw_dict)
    
    json_str1, sha1 = canonical_json(agent)
    json_str2, sha2 = canonical_json(agent)
    
    assert json_str1 == json_str2
    assert sha1 == sha2
    
    # Keys should be sorted in JSON
    # 'agent_type' < 'description' < 'key' < 'model' < 'name' < 'prompt'
    assert json_str1 == '{"agent_type":"llm","description":"desc","key":"test_agent","model":{"name":"qwen"},"name":"Test","prompt":"hello"}'
