"""Factory platform: agents, computers, and the JSON contract.

The agent loop is not wired here yet. Messages open or refuse sessions; the
connector heartbeat marks a computer online. Machine work stays stubbed.
"""

from .api import router
from .store import FactoryStore

__all__ = ["FactoryStore", "router"]
