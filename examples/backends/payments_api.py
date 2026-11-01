"""Mock "payments" backend — a plain FastAPI REST service. The gateway bridges it to MCP by
reading its OpenAPI document; no MCP code lives here. 4 exposed operations.

Requires ``Authorization: Bearer $PAYMENTS_API_TOKEN`` — the gateway injects this upstream
credential, so agents never see it.
"""

from __future__ import annotations

import itertools
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="payments", version="1.0")
_ids = itertools.count(9001)
_charges: dict[str, dict] = {}
METHODS = {"u1": [{"method_id": "pm_visa_4242", "type": "card", "brand": "visa", "last4": "4242"},
                  {"method_id": "pm_wallet", "type": "wallet", "balance_usd": 25.0}]}


def require_token(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("PAYMENTS_API_TOKEN", "dev-payments-token")
    if authorization != f"Bearer {expected}":
        raise HTTPException(401, "invalid upstream credentials")


class ChargeRequest(BaseModel):
    user_id: str
    amount_usd: float = Field(gt=0, le=10_000, description="Amount to charge in USD")
    method_id: str = Field(description="Payment method id from list_payment_methods")
    description: str = ""


class RefundRequest(BaseModel):
    amount_usd: float | None = Field(default=None, gt=0, description="Partial refund amount; omit for full refund")
    reason: str = ""


@app.get("/users/{user_id}/payment-methods", operation_id="list_payment_methods",
         summary="List a user's saved payment methods (cards, wallet)", dependencies=[Depends(require_token)])
def list_payment_methods(user_id: str):
    return {"user_id": user_id, "methods": METHODS.get(user_id, [])}


@app.post("/charges", operation_id="charge_payment", summary="Charge a user's payment method",
          dependencies=[Depends(require_token)])
def charge(req: ChargeRequest):
    cid = f"ch_{next(_ids)}"
    _charges[cid] = {"charge_id": cid, **req.model_dump(), "status": "succeeded", "refunded_usd": 0.0}
    return _charges[cid]


@app.post("/charges/{charge_id}/refund", operation_id="refund_payment",
          summary="Refund a charge fully or partially", openapi_extra={"x-mcp-destructive": True},
          dependencies=[Depends(require_token)])
def refund(charge_id: str, req: RefundRequest):
    ch = _charges.get(charge_id)
    if ch is None:
        raise HTTPException(404, f"charge {charge_id} not found")
    remaining = ch["amount_usd"] - ch["refunded_usd"]
    amount = req.amount_usd or remaining
    if amount > remaining + 1e-9:
        raise HTTPException(400, "refund exceeds remaining charge amount")
    ch["refunded_usd"] = round(ch["refunded_usd"] + amount, 2)
    ch["status"] = "refunded" if ch["refunded_usd"] >= ch["amount_usd"] else "partially_refunded"
    return ch


@app.get("/charges/{charge_id}/invoice", operation_id="get_invoice",
         summary="Get the invoice / receipt for a charge", dependencies=[Depends(require_token)])
def invoice(charge_id: str):
    ch = _charges.get(charge_id)
    if ch is None:
        raise HTTPException(404, f"charge {charge_id} not found")
    return {"invoice_id": "inv_" + charge_id[3:], "charge_id": charge_id, "amount_usd": ch["amount_usd"],
            "refunded_usd": ch["refunded_usd"],
            "line_items": [{"description": ch["description"] or "Charge", "amount_usd": ch["amount_usd"]}]}


def reset() -> None:
    """Clear in-memory state (public demo resets)."""
    _charges.clear()


@app.get("/internal/reconcile", operation_id="reconcile", openapi_extra={"x-mcp-exclude": True},
         dependencies=[Depends(require_token)])
def reconcile():
    """Internal batch job — excluded from MCP via x-mcp-exclude."""
    return {"ok": True}
