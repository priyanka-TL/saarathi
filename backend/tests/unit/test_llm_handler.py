import pytest
import uuid
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, AIMessageChunk
from langchain_core.tools import BaseTool

from app.domain.agent_spec import LlmAgentSpec, ModelSpec, LimitsSpec
from app.domain.core import UserContext
from app.agents.protocol import TurnContext, HistoryTurn
from app.agents.llm_handler import LlmAgentHandler
from app.agents.factory import HandlerDeps

class FakeTool(BaseTool):
    name: str = "fake_tool"
    description: str = "A fake tool"

    def _run(self, *args, **kwargs):
        return "tool_result"

class FakeLLM:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.invocations = []
        
    def bind_tools(self, tools):
        return self
        
    def invoke(self, messages):
        self.invocations.append(messages)
        res = self.responses[self.call_count]
        self.call_count += 1
        return res

class FakeLlmFactory:
    def __init__(self, llm):
        self.llm = llm
    def get(self, model_spec):
        return self.llm

class FakeToolRegistry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
    def resolve(self, tool_names):
        return [self._tools[name] for name in tool_names]

def create_ctx(text: str) -> TurnContext:
    user = UserContext(user_id="u1", email="u1@test", display_name="u", tenant_code="t")
    return TurnContext(
        request_id="req1",
        conversation_id=uuid.uuid4(),
        user=user,
        text=text,
        option_id=None,
        history=[],
        session=None,
        locale="en"
    )

def test_prompt_with_braces_does_not_raise():
    # A prompt containing { and } no longer raises (fixes ChatPromptTemplate issue)
    spec = LlmAgentSpec(
        agent_type="llm", key="test", name="test", description="test",
        prompt="Here is some JSON: { \"key\": \"value\" }",
        model=ModelSpec(name="test_model")
    )
    llm = FakeLLM([AIMessage(content="Hello!")])
    deps = HandlerDeps(
        llm_factory=FakeLlmFactory(llm),
        tool_registry=FakeToolRegistry([]),
        mitra_rest=None, mitra_sessions=None, settings=None
    )
    handler = LlmAgentHandler(spec, deps)
    
    turn = handler.handle(create_ctx("hi"))
    assert turn.text == "Hello!"
    assert len(llm.invocations) == 1
    assert llm.invocations[0][0].content == "Here is some JSON: { \"key\": \"value\" }"


def test_unknown_tool_name_produces_error_toolmessage():
    spec = LlmAgentSpec(
        agent_type="llm", key="test", name="test", description="test",
        prompt="system",
        model=ModelSpec(name="test_model"),
        tools=["fake_tool"]
    )
    
    # Model requests a tool that does not exist
    msg1 = AIMessage(content="", tool_calls=[{"name": "hallucinated_tool", "args": {}, "id": "call_1"}])
    msg2 = AIMessage(content="Final answer")
    
    llm = FakeLLM([msg1, msg2])
    deps = HandlerDeps(
        llm_factory=FakeLlmFactory(llm),
        tool_registry=FakeToolRegistry([FakeTool()]),
        mitra_rest=None, mitra_sessions=None, settings=None
    )
    handler = LlmAgentHandler(spec, deps)
    
    turn = handler.handle(create_ctx("hi"))
    assert turn.text == "Final answer"
    
    # Verify the second invocation has a ToolMessage with an error
    second_invoke_msgs = llm.invocations[1]
    tool_msg = second_invoke_msgs[-1]
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.content == "Unknown tool 'hallucinated_tool'"
    assert tool_msg.tool_call_id == "call_1"
    
    assert len(turn.tool_traces) == 1
    assert turn.tool_traces[0].status == "error"
    assert turn.tool_traces[0].error == "unknown_tool"

def test_tool_loop_bound_is_respected_and_raw_fallback_used():
    spec = LlmAgentSpec(
        agent_type="llm", key="test", name="test", description="test",
        prompt="system",
        model=ModelSpec(name="test_model"),
        tools=["fake_tool"],
        limits=LimitsSpec(max_tool_iterations=3)
    )
    
    # Model keeps calling tools 3 times
    call1 = AIMessage(content="", tool_calls=[{"name": "fake_tool", "args": {}, "id": "c1"}])
    call2 = AIMessage(content="", tool_calls=[{"name": "fake_tool", "args": {}, "id": "c2"}])
    call3 = AIMessage(content="", tool_calls=[{"name": "fake_tool", "args": {}, "id": "c3"}])
    # 4th call (final) returns empty content
    final_call = AIMessage(content="")
    
    llm = FakeLLM([call1, call2, call3, final_call])
    deps = HandlerDeps(
        llm_factory=FakeLlmFactory(llm),
        tool_registry=FakeToolRegistry([FakeTool()]),
        mitra_rest=None, mitra_sessions=None, settings=None
    )
    handler = LlmAgentHandler(spec, deps)
    
    turn = handler.handle(create_ctx("hi"))
    assert turn.text == "Here is the raw data I found:\ntool_result"
    assert len(turn.tool_traces) == 3
    assert turn.tool_traces[0].iteration == 1
    assert turn.tool_traces[2].iteration == 3
    
def test_tool_exceptions_propagate():
    spec = LlmAgentSpec(
        agent_type="llm", key="test", name="test", description="test",
        prompt="system",
        model=ModelSpec(name="test_model"),
        tools=["fake_tool"]
    )
    
    class CrashingTool(BaseTool):
        name: str = "fake_tool"
        description: str = "A fake tool"
        def _run(self, *args, **kwargs):
            raise ValueError("Service unavailable")

    llm = FakeLLM([AIMessage(content="", tool_calls=[{"name": "fake_tool", "args": {}, "id": "c1"}])])
    deps = HandlerDeps(
        llm_factory=FakeLlmFactory(llm),
        tool_registry=FakeToolRegistry([CrashingTool()]),
        mitra_rest=None, mitra_sessions=None, settings=None
    )
    handler = LlmAgentHandler(spec, deps)
    
    with pytest.raises(ValueError, match="Service unavailable"):
        handler.handle(create_ctx("hi"))
