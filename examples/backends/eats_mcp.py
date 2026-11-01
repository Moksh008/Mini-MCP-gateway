"""Mock "eats" backend — a native MCP server spoken over **stdio** (launched by the gateway). 4 tools."""

from __future__ import annotations

import itertools

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("eats")
_ids = itertools.count(501)
RESTAURANTS = {
    "r1": {"id": "r1", "name": "Sakura Sushi", "cuisine": "japanese", "rating": 4.7},
    "r2": {"id": "r2", "name": "Taqueria Sol", "cuisine": "mexican", "rating": 4.5},
    "r3": {"id": "r3", "name": "Bella Napoli", "cuisine": "italian", "rating": 4.6},
}
MENUS = {
    "r1": [{"item_id": "m1", "name": "Salmon roll", "price_usd": 9.5},
           {"item_id": "m2", "name": "Miso soup", "price_usd": 3.0}],
    "r2": [{"item_id": "m3", "name": "Al pastor taco", "price_usd": 3.5},
           {"item_id": "m4", "name": "Burrito", "price_usd": 11.0}],
    "r3": [{"item_id": "m5", "name": "Margherita pizza", "price_usd": 14.0}],
}
_orders: dict[str, dict] = {}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_restaurants(query: str = "", cuisine: str | None = None) -> dict:
    """Find restaurants that deliver food, by name or cuisine."""
    q = query.lower()
    hits = [r for r in RESTAURANTS.values()
            if (not q or q in r["name"].lower() or q in r["cuisine"]) and (not cuisine or r["cuisine"] == cuisine)]
    return {"restaurants": hits}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_menu(restaurant_id: str) -> dict:
    """Get a restaurant's menu items and prices."""
    if restaurant_id not in MENUS:
        raise ValueError(f"restaurant {restaurant_id} not found")
    return {"restaurant_id": restaurant_id, "items": MENUS[restaurant_id]}


@mcp.tool()
def place_order(user_id: str, restaurant_id: str, item_ids: list[str]) -> dict:
    """Place a food delivery order for menu items from one restaurant."""
    menu = {m["item_id"]: m for m in MENUS.get(restaurant_id, [])}
    missing = [i for i in item_ids if i not in menu]
    if missing:
        raise ValueError(f"unknown items: {missing}")
    oid = f"order_{next(_ids)}"
    _orders[oid] = {"order_id": oid, "user_id": user_id, "restaurant_id": restaurant_id,
                    "total_usd": round(sum(menu[i]["price_usd"] for i in item_ids), 2),
                    "status": "preparing", "eta_min": 30}
    return _orders[oid]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def track_order(order_id: str) -> dict:
    """Track the delivery status and ETA of a food order."""
    if order_id not in _orders:
        raise ValueError(f"order {order_id} not found")
    return _orders[order_id]


if __name__ == "__main__":
    mcp.run("stdio")
