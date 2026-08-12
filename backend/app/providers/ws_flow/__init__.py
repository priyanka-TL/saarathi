"""The `ws_flow` protocol family: a conversation held over one pooled,
JSON-framed WebSocket, with a REST surface alongside it for session creation,
completion polling and transcript reads.

Named for the protocol, not for a platform. Two platforms speak it today and
both inherit from `BaseWsFlowProvider` as peers; a third that speaks it needs
only its own `options_model`, its REST client and its `open_session`.

Registers no provider of its own -- there is nothing to enable here.
"""
