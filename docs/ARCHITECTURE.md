# Architecture & reference

## Local development (without Docker)

Requires Python ≥ 3.11 and Node ≥ 20.

```bash
pip install -e ".[dev]"
python -m examples.run_backends                          # terminal 1: rides, support, payments, maps
python -m mcp_gateway serve -c examples/gateway.yaml     # terminal 2: gateway on :8787 (spawns stdio "eats")
cd console && npm install && npm run dev                 # terminal 3: console on :5173 (proxies to :8787)

python -m examples.client_demo                           # MCP client: full vs incremental
python -m experiments.token_usage                        # regenerate experiments/results.md
pytest -q                                                # 76 tests
```

Set `OPENROUTER_API_KEY` in the gateway's environment to enable the agent.

## Components

| Module | Responsibility |
|---|---|
| `mcp_gateway/app.py` | ASGI app: auth middleware, MCP endpoints, REST + admin + agent APIs, demo janitor |
| `mcp_gateway/core.py` | The call pipeline and discovery (list / search / describe / call) |
| `mcp_gateway/mcp_server.py` | `/mcp` (incremental) and `/mcp/full` endpoints; shared meta-tool dispatch |
| `mcp_gateway/registry.py` | Server registry, merged catalog, circuit breakers, demo reset/expiry |
| `mcp_gateway/search.py` | BM25F tool search with light stemming and synonyms |
| `mcp_gateway/policy.py` | RBAC: glob allow/deny (deny wins), read-only roles, admin |
| `mcp_gateway/ratelimit.py` | Token buckets (per request, per caller, per tool, optionally per client IP) |
| `mcp_gateway/audit.py` | Redacted, fingerprinted JSONL audit log + in-memory ring for the API |
| `mcp_gateway/agent.py` | Server-side LLM agent (OpenAI-compatible / OpenRouter) with cost budgets |
| `mcp_gateway/netguard.py` | SSRF guard for runtime-registered upstream URLs |
| `mcp_gateway/upstreams/` | MCP over Streamable HTTP and stdio, REST (declared), OpenAPI → MCP, in-process |
| `console/` | React + TypeScript console (Vite), nginx config, Playwright e2e + demo recorder |

## Call pipeline

`resolve → authorize → rate-limit (caller, then caller+tool) → JSON-Schema validate → circuit breaker + per-upstream
concurrency cap → invoke with timeout → bound result size → metrics + audit`. Invalid calls never reach the upstream.

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `POST /mcp` | key | MCP, **incremental** discovery (`search_tools`, `get_tool_schema`, `call_tool`) |
| `POST /mcp/full` | key | MCP, every permitted tool in `tools/list` (paginated) |
| `GET /v1/meta` | none | Deployment info: version, public-demo settings, agent availability |
| `GET /v1/whoami`, `/v1/tools`, `/v1/tools/search?q=`, `/v1/tools/{name}` | key | REST discovery |
| `POST /v1/tools/{name}/call` | key | REST invocation (same policy, limits and audit as MCP) |
| `POST /v1/agent/run` | key | Run the LLM agent as the caller; streams Server-Sent Events |
| `GET/POST /admin/servers`, `GET/DELETE /admin/servers/{name}` | admin | Registry: list, register (`?replace=true`), deregister |
| `POST /admin/servers/{name}/refresh\|enable\|disable` | admin | Registry operations |
| `GET /admin/tools?q=&server=` | admin | Full catalog view |
| `GET /admin/audit?principal=&event=&tool=&outcome=` | admin | Recent audit events |
| `GET /admin/stats` | admin | JSON metrics snapshot |
| `GET /admin/policy/matrix` | admin | Effective permission of every configured principal × tool, with reasons |
| `GET /healthz`, `/readyz`, `/metrics` | none* | Liveness, readiness, Prometheus (*admin when `metrics_public: false`) |

Every response carries `X-Request-ID` (a valid incoming one is propagated). The same ID appears in the audit log and
in tool results' `_meta`.

## Security model

* **API keys** are stored only as SHA-256 digests and compared in constant time (`mcp-gateway gen-key` mints them).
* **No existence oracle:** unknown and forbidden tools return the same error; search and `get_tool_schema` only show
  tools the caller may use. The audit log records the real reason.
* **Upstream credentials** (`Authorization: Bearer ${PAYMENTS_API_TOKEN}`) are expanded from the environment at
  connect time — never stored, never returned, never seen by agents.
* **Read-only roles** use MCP `readOnlyHint`. REST bridges infer it (GET = read-only); OpenAPI operations can override
  with `x-mcp-read-only` / `x-mcp-destructive` or opt out with `x-mcp-exclude`.
* **SSRF:** runtime-registered URLs must be http(s); metadata hostnames and link-local / unspecified / reserved
  addresses are always rejected (on every resolved address); `upstream_host_allowlist` restricts targets further.
  Runtime `mcp_stdio` registration is off by default (it would be remote code execution).
* **Audit** arguments are redacted by key, truncated and fingerprinted (`args_sha256`).
* **Bounded metric labels:** calls to unknown tool names are recorded as `(unknown)`, so callers can't inflate
  metric cardinality.
* **Console:** same-origin through nginx, strict CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; operator
  keys live only in the tab's `sessionStorage`.

## Public demo mode

Enabled by layering [`deploy/demo.overlay.yaml`](../deploy/demo.overlay.yaml) on the base config. Designed so the demo
keys can be published:

| Risk | Control |
|---|---|
| Everyone shares the same keys | Rate limits keyed by principal **and client IP** (`limits.per_client_ip`) |
| Client spoofs its IP | Caddy sets `X-Forwarded-For`; nginx trusts it only from private networks and forwards a single overwritten value |
| Visitors break the demo | Registry resets to config every 30 min; runtime servers expire after 10 min; max 3 at a time; backends wipe data |
| SSRF via "register a server" | Host allowlist (`backends`, `127.0.0.1`) + metadata/link-local blocking |
| Privacy | Audit API hides other visitors' IP addresses |
| LLM bill | Agent: per-IP run rate, daily run and token budgets, max steps/tokens, model allowlist; nginx limit on `/v1/agent/run` |
| Exposure | Only Caddy (80/443) is published; gateway, console and backends stay on the private Docker network |

## The LLM agent

`POST /v1/agent/run {task, mode, model}` runs a tool-calling loop against an OpenAI-compatible chat-completions API
(OpenRouter by default). It acts **as the calling principal**: in `search` mode it is offered only the three meta-tools
and goes through the exact same dispatch code as `/mcp`; in `full` mode it is offered the caller's whole catalog. Each
LLM call reports the provider's own token usage, so comparing modes gives tokenizer-exact numbers. Every run is
audited (`agent.run`) with tokens, tools used and outcome.

## Configuration

See [`examples/gateway.yaml`](../examples/gateway.yaml) (fully commented). Multiple files can be layered:
`mcp-gateway serve -c base.yaml -c overlay.yaml` (or `MCPGW_CONFIG=base.yaml,overlay.yaml`); mappings merge
recursively, lists replace. Endpoint URLs accept `${VAR:-default}`. Validate with `mcp-gateway check-config -c …`.

## Testing

* `pytest -q` — **76 tests**: unit (policy, token buckets, auth, config, audit redaction, circuit breaker, search,
  OpenAPI conversion, SSRF guard, registry reset/expiry, result truncation, metrics) and end-to-end (real uvicorn
  servers for every upstream kind behind a real gateway, driven by the official MCP client; the agent against a
  scripted OpenAI-compatible mock; a second gateway in public-demo mode).
* `npm run build` in `console/` — strict TypeScript typecheck + production build.
* `python console/e2e/ui_smoke.py --url …` — drives the real UI in a headless browser against the running stack
  (playground, agent page, every operator page, all 6 scenarios must PASS, register/remove a server); fails on any
  browser console error including CSP violations. CI runs it against `docker compose` on every push.

## Scaling beyond one machine

Deliberately single-node today, with seams for: Redis-backed `RateLimiter`; Postgres registry with change
notifications between replicas; audit sink to Kafka / a log pipeline; OIDC/JWT + mTLS identity and per-user OAuth
token exchange to upstreams; hybrid lexical + embedding search learned from usage; OpenTelemetry tracing.
