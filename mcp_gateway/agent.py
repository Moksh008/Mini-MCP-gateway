"""Built-in LLM agent that uses the gateway as its only tool source.

The agent acts *as the calling principal*: every tool it uses goes through the same
discovery, RBAC, rate limits, validation and audit as any MCP client. It speaks the
OpenAI-compatible chat-completions protocol, so it works with OpenRouter (default) or any
compatible endpoint, and reports the provider's own token counts — which makes it a live,
tokenizer-exact version of the full-vs-incremental discovery experiment.

Cost controls (important when the demo is public): per-caller run rate, a daily run budget,
a daily token budget, bounded steps and completion tokens, and truncated tool results.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp import types

from .config import AgentSpec
from .core import Gateway, to_mcp_tool
from .errors import GatewayError, InvalidArguments, RateLimited
from .mcp_server import META_TOOLS, SEARCH_INSTRUCTIONS, dispatch_search_mode, error_result
from .models import CallContext

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a capable assistant for a ride-hailing and delivery platform, acting on behalf of \
the signed-in customer (user id "u1", rider id "u1"). You can only act through the provided tools.

Rules:
- Use tools to look things up and take actions; never invent tool results, IDs or prices.
- If a tool is denied or not found, say plainly that you don't have permission or the capability — do not retry \
the same call.
- Prefer the fewest tool calls that complete the task. Ask no follow-up questions: make reasonable assumptions \
(e.g. economy product, the customer's first saved card) and state them.
- Finish with a short, friendly summary of what you did and the key results.
Today is {today}."""


def _to_openai_tool(tool: types.Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


@dataclass
class _Budget:
    day: str = ""
    runs: int = 0
    tokens: int = 0

    def roll(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.runs, self.tokens = today, 0, 0


@dataclass
class RunTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tools_used: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, spec: AgentSpec, gateway: Gateway, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.spec = spec
        self.gateway = gateway
        self._budget = _Budget()
        self._transport = transport

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.spec.api_key_env) or None

    @property
    def available(self) -> bool:
        return self.spec.enabled and bool(self.api_key)

    def info(self) -> dict[str, Any]:
        self._budget.roll()
        return {
            "available": self.available,
            "reason": None if self.available else (
                "disabled in config" if not self.spec.enabled else f"set {self.spec.api_key_env} to enable"),
            "default_model": self.spec.model,
            "models": self.spec.allowed_models,
            "max_steps": self.spec.max_steps,
            "daily_runs_left": max(0, self.spec.daily_run_budget - self._budget.runs),
            "daily_tokens_left": max(0, self.spec.daily_token_budget - self._budget.tokens),
        }

    # ---- admission -------------------------------------------------------------------
    def admit(self, ctx: CallContext, model: str | None) -> str:
        """Validate and charge a run against limits. Returns the model to use."""
        if not self.available:
            raise GatewayError("the agent is not configured on this gateway")
        model = model or self.spec.model
        if model not in self.spec.allowed_models:
            raise InvalidArguments(f"model must be one of {self.spec.allowed_models}")
        key = f"agent:{self.gateway.rate_key(ctx.principal, ctx.client_ip)}"
        wait = self.gateway.limiter.acquire(key, self.spec.run_rate)
        if wait > 0:
            self.gateway.metrics.rate_limited.inc("agent")
            raise RateLimited(f"agent run limit reached; retry after {wait:.0f}s", retry_after=wait)
        self._budget.roll()
        if self._budget.runs >= self.spec.daily_run_budget or self._budget.tokens >= self.spec.daily_token_budget:
            raise RateLimited("the demo's daily agent budget is used up; try again tomorrow", retry_after=3600)
        self._budget.runs += 1
        return model

    # ---- run -------------------------------------------------------------------------
    async def run(self, ctx: CallContext, task: str, mode: str, model: str) -> AsyncIterator[dict[str, Any]]:
        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        totals = RunTotals()
        if mode == "search":
            tools = [_to_openai_tool(t) for t in META_TOOLS]
            system = SYSTEM_PROMPT.format(today=datetime.now(UTC).date()) + "\n\n" + SEARCH_INSTRUCTIONS
        else:
            tools = [_to_openai_tool(to_mcp_tool(t)) for t in self.gateway.visible_tools(ctx.principal)]
            system = SYSTEM_PROMPT.format(today=datetime.now(UTC).date())
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        yield {"type": "start", "run_id": run_id, "model": model, "mode": mode, "tools_offered": len(tools),
               "tool_definition_bytes": len(json.dumps(tools)), "principal": ctx.principal.id}

        final_text, error = None, None
        try:
            async with httpx.AsyncClient(timeout=self.spec.timeout_s, transport=self._transport) as client:
                for step in range(1, self.spec.max_steps + 1):
                    t0 = time.perf_counter()
                    msg, usage = await self._complete(client, model, messages, tools)
                    totals.llm_calls += 1
                    totals.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    totals.completion_tokens += int(usage.get("completion_tokens") or 0)
                    totals.cost_usd += float(usage.get("cost") or 0.0)
                    calls = msg.get("tool_calls") or []
                    yield {"type": "llm", "step": step, "ms": round((time.perf_counter() - t0) * 1000),
                           "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                           "cost_usd": usage.get("cost"), "text": msg.get("content") or "",
                           "tool_calls": [c["function"]["name"] for c in calls]}
                    messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
                    if not calls:
                        final_text = msg.get("content") or ""
                        break
                    for call in calls:
                        event, content = await self._execute(ctx, mode, call, totals)
                        yield {"step": step, **event}
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
                else:
                    final_text = f"(stopped after {self.spec.max_steps} steps without a final answer)"
        except _LLMError as e:
            error = str(e)
        except httpx.HTTPError as e:
            error = f"LLM request failed: {type(e).__name__}"
        finally:
            self._budget.tokens += totals.prompt_tokens + totals.completion_tokens
            ms = round((time.perf_counter() - started) * 1000)
            self.gateway.audit.log(
                "agent.run", request_id=ctx.request_id, principal=ctx.principal.id, mode=mode, model=model,
                client_ip=ctx.client_ip, outcome="error" if error else "ok", task=task[:300], latency_ms=ms,
                prompt_tokens=totals.prompt_tokens, completion_tokens=totals.completion_tokens,
                llm_calls=totals.llm_calls, tool_calls=totals.tool_calls, tools_used=totals.tools_used or None,
                error=error,
            )
        summary = {"prompt_tokens": totals.prompt_tokens, "completion_tokens": totals.completion_tokens,
                   "cost_usd": round(totals.cost_usd, 6), "llm_calls": totals.llm_calls, "tool_calls": totals.tool_calls,
                   "tool_errors": totals.tool_errors, "tools_used": totals.tools_used, "ms": ms}
        if error:
            yield {"type": "error", "message": error, "totals": summary}
        else:
            yield {"type": "final", "text": final_text, "totals": summary}

    async def _complete(self, client: httpx.AsyncClient, model: str, messages: list, tools: list) -> tuple[dict, dict]:
        resp = await client.post(
            f"{self.spec.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "HTTP-Referer": self.spec.app_url,
                     "X-Title": self.spec.app_name},
            json={"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                  "max_tokens": self.spec.max_tokens, "temperature": 0, "usage": {"include": True}},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or "choices" not in body:
            err = body.get("error") if isinstance(body, dict) else None
            detail = err.get("message") if isinstance(err, dict) else resp.text[:200]
            log.warning("LLM error %s: %s", resp.status_code, detail)
            raise _LLMError(f"LLM provider returned HTTP {resp.status_code}: {detail}")
        return body["choices"][0]["message"], body.get("usage") or {}

    async def _execute(self, ctx: CallContext, mode: str, call: dict, totals: RunTotals) -> tuple[dict, str]:
        name = call["function"]["name"]
        t0 = time.perf_counter()
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            totals.tool_errors += 1
            content = f"[invalid_arguments] could not parse tool arguments as JSON: {e}"
            return {"type": "tool", "name": name, "arguments": call["function"].get("arguments"), "is_error": True,
                    "result": content, "ms": 0}, content
        target = args.get("name") if mode == "search" and name == "call_tool" else (None if mode == "search" else name)
        try:
            result = await (dispatch_search_mode(self.gateway, ctx, name, args) if mode == "search"
                            else self.gateway.call(ctx, name, args))
        except GatewayError as e:
            result = error_result(e)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if len(text) > self.spec.max_tool_result_chars:
            text = text[: self.spec.max_tool_result_chars] + "…[truncated]"
        totals.tool_calls += 1
        if result.isError:
            totals.tool_errors += 1
        if target and not result.isError:
            totals.tools_used.append(target)
        event = {"type": "tool", "name": name, "target": target, "arguments": args, "is_error": bool(result.isError),
                 "result": text, "ms": round((time.perf_counter() - t0) * 1000)}
        return event, text


class _LLMError(Exception):
    pass
