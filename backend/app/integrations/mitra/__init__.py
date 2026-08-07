"""The Mitra integration.

Responsible for: everything that talks to the external interview platform --
REST client, WebSocket channel, channel pool, connection resolution.
Used by: RemoteFlowAgentHandler and the turn pipeline.

Deliberately exports nothing: submodules are imported directly, so enabling
Mitra does not pull the WebSocket stack into a deployment that has it off.
"""
