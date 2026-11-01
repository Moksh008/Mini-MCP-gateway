"""Mock "support" backend — a native MCP server (Streamable HTTP). 3 tools."""

from __future__ import annotations

import itertools
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("support", stateless_http=True, json_response=True,
              host=os.environ.get("BACKENDS_HOST", "127.0.0.1"))
_ids = itertools.count(1)
_tickets: dict[str, dict] = {}


@mcp.tool()
def create_ticket(user_id: str, subject: str, body: str,
                  category: Literal["ride", "order", "payment", "account", "safety"] = "ride") -> dict:
    """Open a customer support ticket for a complaint or issue."""
    tid = f"T-{next(_ids):05d}"
    _tickets[tid] = {"ticket_id": tid, "user_id": user_id, "subject": subject, "body": body,
                     "category": category, "status": "open", "priority": "normal"}
    return _tickets[tid]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_ticket(ticket_id: str) -> dict:
    """Get a support ticket's status and details."""
    if ticket_id not in _tickets:
        raise ValueError(f"ticket {ticket_id} not found")
    return _tickets[ticket_id]


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def escalate_ticket(ticket_id: str, reason: str) -> dict:
    """Escalate a support ticket to a human agent with urgent priority."""
    if ticket_id not in _tickets:
        raise ValueError(f"ticket {ticket_id} not found")
    _tickets[ticket_id].update(priority="urgent", status="escalated", escalation_reason=reason)
    return _tickets[ticket_id]


def reset() -> None:
    """Clear in-memory state (public demo resets)."""
    _tickets.clear()


app = mcp.streamable_http_app()
