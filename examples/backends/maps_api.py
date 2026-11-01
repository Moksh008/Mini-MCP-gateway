"""Mock "maps" backend — a legacy REST service with **no** OpenAPI document. The gateway
config declares its operations explicitly (kind: rest). 4 operations."""

from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="maps", openapi_url=None, docs_url=None, redoc_url=None)
PLACES = {
    "1455 market st, san francisco": (37.7749, -122.4194),
    "sfo airport": (37.6213, -122.3790),
    "ferry building, san francisco": (37.7955, -122.3937),
}


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat, dlng = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


@app.get("/v1/geocode")
def geocode(address: str):
    key = address.strip().lower()
    for name, (lat, lng) in PLACES.items():
        if key in name or name in key:
            return {"address": name, "lat": lat, "lng": lng}
    raise HTTPException(404, f"address not found: {address}")


@app.get("/v1/reverse-geocode")
def reverse_geocode(lat: float, lng: float):
    name = min(PLACES, key=lambda n: _km(PLACES[n], (lat, lng)))
    return {"address": name, "lat": lat, "lng": lng}


class RouteQuery(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    mode: str = "driving"


@app.post("/v1/eta")
def eta(q: RouteQuery):
    km = _km((q.origin_lat, q.origin_lng), (q.dest_lat, q.dest_lng))
    speed = {"driving": 35, "walking": 5, "cycling": 15}.get(q.mode, 35)
    return {"distance_km": round(km, 2), "eta_min": round(km / speed * 60, 1), "mode": q.mode}


@app.post("/v1/route")
def route(q: RouteQuery):
    km = _km((q.origin_lat, q.origin_lng), (q.dest_lat, q.dest_lng))
    steps = [{"instruction": "Head north", "km": round(km * 0.3, 2)},
             {"instruction": "Continue onto highway", "km": round(km * 0.6, 2)},
             {"instruction": "Arrive at destination", "km": round(km * 0.1, 2)}]
    return {"distance_km": round(km, 2), "steps": steps, "polyline": "mock_polyline"}
