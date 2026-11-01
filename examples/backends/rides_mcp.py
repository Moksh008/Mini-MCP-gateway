"""Mock "rides" backend — a native MCP server (Streamable HTTP). 5 tools."""

from __future__ import annotations

import itertools
import math
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("rides", stateless_http=True, json_response=True,
              host=os.environ.get("BACKENDS_HOST", "127.0.0.1"))
_ids = itertools.count(1001)
_rides: dict[str, dict] = {}
RATES = {"economy": (2.5, 1.2), "comfort": (3.5, 1.8), "xl": (4.0, 2.3), "black": (7.0, 3.5)}
ProductType = Literal["economy", "comfort", "xl", "black"]


def _distance_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    dlat, dlng = math.radians(b_lat - a_lat), math.radians(b_lng - a_lng)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def estimate_fare(pickup_lat: float, pickup_lng: float, dropoff_lat: float, dropoff_lng: float,
                  product: ProductType = "economy") -> dict:
    """Estimate the price of a ride between two coordinates for a product type, before booking."""
    km = _distance_km(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng)
    base, per_km = RATES[product]
    fare = round(base + per_km * km, 2)
    return {"product": product, "distance_km": round(km, 2), "fare_usd": fare,
            "range_usd": [round(fare * 0.9, 2), round(fare * 1.15, 2)]}


@mcp.tool()
def request_ride(rider_id: str, pickup_lat: float, pickup_lng: float, dropoff_lat: float, dropoff_lng: float,
                 product: ProductType = "economy") -> dict:
    """Book a ride for a rider from a pickup point to a drop-off point. Returns the ride id and driver ETA."""
    ride_id = f"ride_{next(_ids)}"
    fare = estimate_fare(pickup_lat, pickup_lng, dropoff_lat, dropoff_lng, product)
    _rides[ride_id] = {"ride_id": ride_id, "rider_id": rider_id, "status": "driver_assigned",
                       "product": product, "fare_usd": fare["fare_usd"], "driver_eta_min": 4}
    return _rides[ride_id]


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
def cancel_ride(ride_id: str, reason: str = "") -> dict:
    """Cancel an active ride. A cancellation fee may apply after the driver is assigned."""
    ride = _rides.get(ride_id)
    if ride is None:
        raise ValueError(f"ride {ride_id} not found")
    ride["status"] = "cancelled"
    return {"ride_id": ride_id, "status": "cancelled", "fee_usd": 5.0 if ride["product"] != "economy" else 0.0}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_ride_status(ride_id: str) -> dict:
    """Get the current status, driver ETA and fare of a ride."""
    ride = _rides.get(ride_id)
    if ride is None:
        raise ValueError(f"ride {ride_id} not found")
    return ride


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_ride_history(rider_id: str, limit: int = 10) -> dict:
    """List a rider's past trips, most recent first."""
    trips = [r for r in _rides.values() if r["rider_id"] == rider_id][::-1][:limit]
    return {"rider_id": rider_id, "trips": trips}


def reset() -> None:
    """Clear in-memory state (public demo resets)."""
    _rides.clear()


app = mcp.streamable_http_app()
