"""A scripted OpenAI-compatible chat-completions server for testing the agent offline.

Search mode:  search_tools(task) → get_tool_schema(top hit) → call_tool(top hit, canned args) → answer.
Full mode:    call the matching tool directly → answer.
Tasks mentioning "refund" try payments__refund_payment directly (to exercise RBAC on the agent).
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request

ARGS = {
    "rides__estimate_fare": {"pickup_lat": 37.7749, "pickup_lng": -122.4194, "dropoff_lat": 37.6213, "dropoff_lng": -122.379},
    "payments__refund_payment": {"charge_id": "ch_nope"},
}

app = FastAPI()
app.state.requests = []


def _call(name: str, args: dict, n: int) -> dict:
    return {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(message: dict, body: dict) -> dict:
    prompt = len(json.dumps(body["messages"])) // 4 + len(json.dumps(body.get("tools", []))) // 4
    return {"id": "cmpl", "model": body["model"], "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": 20, "total_tokens": prompt + 20, "cost": 0.0001}}


@app.post("/chat/completions")
async def completions(request: Request):
    body = await request.json()
    app.state.requests.append({"auth": request.headers.get("authorization"), "body": body})
    msgs = body["messages"]
    task = next(m["content"] for m in msgs if m["role"] == "user")
    tool_names = {t["function"]["name"] for t in body.get("tools", [])}
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    n = len(tool_msgs)
    search_mode = "search_tools" in tool_names

    if tool_msgs and (n >= 3 or not search_mode or "refund" in task):
        return _reply({"role": "assistant", "content": f"Done. Last result: {tool_msgs[-1]['content'][:120]}"}, body)
    if "refund" in task:
        name = "call_tool" if search_mode else "payments__refund_payment"
        args = {"name": "payments__refund_payment", "arguments": ARGS["payments__refund_payment"]} if search_mode \
            else ARGS["payments__refund_payment"]
        return _reply({"role": "assistant", "content": "", "tool_calls": [_call(name, args, n)]}, body)
    if not search_mode:
        return _reply({"role": "assistant", "content": "", "tool_calls": [
            _call("rides__estimate_fare", ARGS["rides__estimate_fare"], n)]}, body)
    if n == 0:
        return _reply({"role": "assistant", "content": "Let me find a tool.", "tool_calls": [
            _call("search_tools", {"query": task, "limit": 3}, n)]}, body)
    if n == 1:
        top = json.loads(tool_msgs[0]["content"])["results"][0]["name"]
        return _reply({"role": "assistant", "content": "", "tool_calls": [_call("get_tool_schema", {"names": [top]}, n)]}, body)
    top = json.loads(tool_msgs[1]["content"])["tools"][0]["name"]
    return _reply({"role": "assistant", "content": "", "tool_calls": [
        _call("call_tool", {"name": top, "arguments": ARGS.get(top, {})}, n)]}, body)
