"""The LLM agent handler.

Responsible for: building the message list, calling the model, and running the
tool loop up to `limits.max_tool_iterations`.
Used by: HandlerFactory, for any spec with agent_type="llm".

Messages are built EXPLICITLY rather than through a ChatPromptTemplate, so what
reaches the model is readable here and history is bounded by the spec's memory.
"""
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages import BaseMessage

from app.core import timing
from app.domain.agent_spec import LlmAgentSpec
from app.agents.protocol import AgentHandler, AgentTurn, ToolTrace, TurnContext
from app.agents.factory import register_handler, HandlerDeps

def _as_text(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, list):
        # some models return list of blocks
        return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content) if content else ""

def _usage(msg: BaseMessage) -> dict:
    if hasattr(msg, "response_metadata") and msg.response_metadata and "token_usage" in msg.response_metadata:
        usage_info = msg.response_metadata["token_usage"]
        return {
            "prompt_tokens": usage_info.get("prompt_tokens"),
            "completion_tokens": usage_info.get("completion_tokens"),
        }
    return {}

@register_handler
class LlmAgentHandler:
    agent_type = "llm"

    def __init__(self, spec: LlmAgentSpec, deps: HandlerDeps):
        self._spec = spec
        self._llm = deps.llm_factory.get(spec.model)
        self._tools = deps.tool_registry.resolve(spec.tools) if spec.tools else []
        self._tools_by_name = {t.name: t for t in self._tools}
        self._bound = self._llm.bind_tools(self._tools) if self._tools else self._llm

    def _invoke(self, msgs):
        """One model call, timed and counted.

        A ONE-LINE WRAPPER RATHER THAN THREE `with` BLOCKS, because the three
        call sites below are the same measurement and a tool-using turn visits
        two of them repeatedly. `stage` accumulates and `count` increments, so a
        turn that looped four times reports the total model time and `llm_calls:
        4` -- which is the number that says whether a slow turn was one slow
        call or several quick ones.
        """
        timing.count("llm_calls")
        with timing.stage("llm"):
            return self._bound.invoke(msgs)

    def handle(self, ctx: TurnContext) -> AgentTurn:
        t0 = timing.start()

        # Build messages EXPLICITLY. No ChatPromptTemplate.
        msgs: list[BaseMessage] = [SystemMessage(content=self._spec.prompt)]
        
        if self._spec.memory.strategy == "recent":
            history_turns = self._spec.memory.history_turns
            if history_turns > 0:
                for h in ctx.history[-2 * history_turns:]:
                    msgs.append(HumanMessage(content=h.content) if h.role == "user" else AIMessage(content=h.content))
                    
        msgs.append(HumanMessage(content=ctx.text))

        if not self._tools:
            r = self._invoke(msgs)
            text = _as_text(r) or "I couldn't generate a clear answer."
            latency_ms = timing.elapsed_ms(t0)
            return AgentTurn(text=text, model=self._spec.model.name, latency_ms=latency_ms, **_usage(r))

        traces: list[ToolTrace] = []
        last_result = ""
        
        for i in range(1, self._spec.limits.max_tool_iterations + 1):
            r = self._invoke(msgs)

            if not r.tool_calls:
                text = _as_text(r) or "I couldn't generate a clear answer."
                latency_ms = timing.elapsed_ms(t0)
                return AgentTurn(text=text, tool_traces=traces, model=self._spec.model.name, latency_ms=latency_ms, **_usage(r))
                
            msgs.append(r)
            
            for call in r.tool_calls:
                tstart = timing.start()
                tool_name = call["name"]
                tool_args = call["args"]
                tool = self._tools_by_name.get(tool_name)
                
                if tool is None:
                    out = f"Unknown tool {tool_name!r}"
                    status = "error"
                    err = "unknown_tool"
                else:
                    try:
                        out = str(tool.invoke(tool_args))
                        status = "success"
                        err = None
                    except Exception as e:
                        # Propagate exception to service layer instead of stringifying into answer
                        raise e

                duration_ms = timing.elapsed_ms(tstart)
                # Already on the ToolTrace row; also filed as a stage so the
                # turn's log line adds up without joining to tool_executions.
                timing.record("tools", duration_ms)
                timing.count("tool_calls")
                traces.append(ToolTrace(
                    tool_name=tool_name, 
                    iteration=i, 
                    arguments=tool_args, 
                    result_excerpt=out[:4096],
                    status=status, 
                    error=err, 
                    duration_ms=duration_ms
                ))
                last_result = out
                msgs.append(ToolMessage(content=out, tool_call_id=call["id"]))

        final = self._invoke(msgs)
        text = _as_text(final)
        if not text:
            text = f"Here is the raw data I found:\n{last_result}"
            
        latency_ms = timing.elapsed_ms(t0)
        return AgentTurn(text=text, tool_traces=traces, model=self._spec.model.name, latency_ms=latency_ms, **_usage(final))
