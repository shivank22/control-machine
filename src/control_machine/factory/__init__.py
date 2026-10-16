"""Factory platform: agents, computers, and the JSON contract.

Messages open or refuse sessions. The connector heartbeat marks a computer
online. The outbound connector stream delivers tool calls; the Deep Agent
loop that decides those calls is not wired here yet.
"""

from .api import router
from .connector import ConnectorError, ConnectorHub, ToolCall, ToolResult
from .store import FactoryStore

__all__ = [
    "ConnectorError",
    "ConnectorHub",
    "FactoryStore",
    "ToolCall",
    "ToolResult",
    "router",
]
