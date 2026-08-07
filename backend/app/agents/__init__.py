"""Agent handlers.

The live surface is `protocol.AgentHandler` (the Protocol every handler
satisfies) and `factory.HandlerFactory` (which builds one from an AgentSpec).
Nothing is re-exported here: `factory.py` auto-imports the handler modules with
pkgutil for their registration side effect, so an eager re-export would only
duplicate that and reintroduce an import cycle risk.

The legacy `BaseAgent` ABC that used to live in `base.py` is gone. It was
unreachable -- `HandlerFactory` builds `LlmAgentHandler` / `RemoteFlowAgentHandler`
directly -- and it swallowed provider exceptions, printed a traceback to stderr,
and returned the raw exception string to the user as the assistant's reply with
HTTP 200.
"""
