"""Experiment: context-token cost of full vs incremental tool discovery.

Part A (live): measures the real payloads produced by a running gateway with 20 tools across
5 upstreams — `tools/list` on /mcp/full versus the meta-tools plus search/get_tool_schema
round-trips on /mcp — for 10 realistic agent tasks, and checks search recall.

Part B (scale): the same 10 tasks against catalogs padded with synthetic distractor tools
(100 → 5,000 tools, the scale Uber reports) to show how both costs and search accuracy
behave as the catalog grows.

    python -m examples.run_backends &          # terminal 1
    python -m mcp_gateway serve -c examples/gateway.yaml   # terminal 2
    python -m experiments.token_usage          # writes experiments/results.{md,json}

Token counts use tiktoken's o200k_base encoding as a provider-neutral proxy; absolute numbers
differ per model tokenizer, but the ratios are what matter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import tiktoken
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from mcp_gateway.mcp_server import META_TOOLS
from mcp_gateway.models import ToolRecord
from mcp_gateway.search import ToolSearchIndex

ENC = tiktoken.get_encoding("o200k_base")
OUT_DIR = Path(__file__).resolve().parent


def tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return len(ENC.encode(text))


def model_tool_def(t) -> dict:
    """The shape a model API receives for one tool (name, description, input_schema)."""
    return {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}


# Each task: list of (search query the agent would issue, tool it needs).
TASKS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Estimate fare Market St → SFO", [("estimate ride fare price", "rides__estimate_fare")]),
    ("Book a comfort ride", [("book a ride", "rides__request_ride")]),
    ("Cancel my current ride", [("cancel ride", "rides__cancel_ride")]),
    ("Where is my food order?", [("track food order delivery status", "eats__track_order")]),
    ("Order sushi for dinner", [("find restaurants sushi", "eats__search_restaurants"),
                                ("restaurant menu items", "eats__get_menu"),
                                ("place food order", "eats__place_order")]),
    ("Refund the last charge", [("refund a charge", "payments__refund_payment")]),
    ("Send me my receipt", [("receipt invoice for charge", "payments__get_invoice")]),
    ("How long to drive to Ferry Building?", [("address to coordinates", "maps__geocode"),
                                              ("travel time eta between coordinates", "maps__get_eta")]),
    ("Complain about my driver", [("open support ticket complaint", "support__create_ticket")]),
    ("Show my past trips", [("rider trip history", "rides__list_ride_history")]),
]


@dataclass
class TaskCost:
    task: str
    tools_needed: int
    full_total: int
    incr_total: int
    discovery_tokens: int
    recall_at_1: float
    recall_at_5: float


def simulate(
    task: str, steps: list[tuple[str, str]], full_defs_tokens: int, meta_defs_tokens: int,
    search_io: list[int], schema_io: list[int], call_overhead: list[int], hits: list[list[str]],
) -> TaskCost:
    """Total *input* tokens spent on tool definitions + discovery across the agent loop.

    Full mode:        k call turns + 1 final turn, each resending all definitions.
    Incremental mode: per needed tool a search turn, a schema turn and a call turn (+1 final);
                      each turn resends the 3 meta-tools plus all discovery I/O so far.
    Conversation text and tool *results* are identical in both modes, so they are excluded.
    """
    k = len(steps)
    full_total = (k + 1) * full_defs_tokens
    incr_total, transcript = 0, 0
    for i in range(k):
        for delta in (search_io[i], schema_io[i], call_overhead[i]):
            incr_total += meta_defs_tokens + transcript
            transcript += delta
    incr_total += meta_defs_tokens + transcript  # final answer turn
    r1 = sum(1 for (_, exp), h in zip(steps, hits, strict=True) if h[:1] == [exp]) / k
    r5 = sum(1 for (_, exp), h in zip(steps, hits, strict=True) if exp in h[:5]) / k
    return TaskCost(task, k, full_total, incr_total, sum(search_io) + sum(schema_io), r1, r5)


async def part_a(url: str, key: str) -> dict:
    headers = {"Authorization": f"Bearer {key}"}
    async with streamablehttp_client(f"{url}/mcp/full", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            full_tools = (await s.list_tools()).tools
    full_defs = [model_tool_def(t) for t in full_tools]
    full_tokens = tokens(full_defs)
    per_tool = sorted(((t["name"], tokens(t)) for t in full_defs), key=lambda x: -x[1])
    meta_tokens = tokens([model_tool_def(t) for t in META_TOOLS])

    results: list[TaskCost] = []
    async with streamablehttp_client(f"{url}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for task, steps in TASKS:
                search_io, schema_io, call_io, hits = [], [], [], []
                for query, expected in steps:
                    args = {"query": query, "limit": 5}
                    res = await s.call_tool("search_tools", args)
                    res_text = "".join(c.text for c in res.content)
                    names = [h["name"] for h in json.loads(res_text)["results"]]
                    hits.append(names)
                    search_io.append(tokens({"name": "search_tools", "input": args}) + tokens(res_text))
                    chosen = expected if expected in names else (names[0] if names else expected)
                    sargs = {"names": [chosen]}
                    sres = await s.call_tool("get_tool_schema", sargs)
                    schema_io.append(tokens({"name": "get_tool_schema", "input": sargs})
                                     + tokens("".join(c.text for c in sres.content)))
                    # call_tool wraps the real arguments: only the wrapper is extra vs. full mode
                    call_io.append(tokens({"name": "call_tool", "input": {"name": chosen, "arguments": {}}})
                                   - tokens({"name": chosen, "input": {}}))
                results.append(simulate(task, steps, full_tokens, meta_tokens, search_io, schema_io, call_io, hits))
    return {
        "tool_count": len(full_tools),
        "full_definitions_tokens": full_tokens,
        "meta_definitions_tokens": meta_tokens,
        "avg_tokens_per_tool": round(full_tokens / len(full_tools), 1),
        "largest_tools": per_tool[:5],
        "tasks": [r.__dict__ for r in results],
    }


# ---------------------------------------------------------------------------------------
# Part B — synthetic scale-out
# ---------------------------------------------------------------------------------------
DOMAINS = ["freight", "fleet", "driver-onboarding", "tax", "promotions", "safety", "identity", "insurance",
           "pricing-ops", "marketplace", "courier", "transit", "rentals", "vouchers", "loyalty", "compliance",
           "kyc", "fraud", "ads", "merchant", "grocery", "catalog", "inventory", "warehouse", "hr", "finance",
           "treasury", "legal", "localization", "notifications", "experiments", "analytics", "billing-ops",
           "partners", "dispatch", "telematics", "maintenance", "chargers", "scooters", "bikes"]
VERBS = ["get", "list", "create", "update", "delete", "search", "approve", "reject", "export", "sync",
         "validate", "schedule", "archive", "assign", "audit"]
# Half the entities deliberately overlap with the real tools' vocabulary (ride, fare, order, invoice…)
# so distractors compete lexically with the right answer — otherwise recall would be trivially 100%.
ENTITIES = ["record", "report", "policy", "document", "account", "batch", "job", "rule", "config", "contract",
            "ride", "trip", "fare", "order", "ticket", "invoice", "charge", "payment", "restaurant", "menu",
            "route", "eta", "refund", "delivery", "address", "receipt", "complaint", "history", "price", "driver"]


def synthetic_tools(n: int, seed: int = 7) -> list[ToolRecord]:
    rng = random.Random(seed)
    combos = [(d, v, e) for d in DOMAINS for v in VERBS for e in ENTITIES]
    rng.shuffle(combos)
    out = []
    for d, v, e in combos[:n]:
        props = {f"{e}_id": {"type": "string", "description": f"Identifier of the {e}"},
                 "region": {"type": "string", "enum": ["us", "eu", "apac", "latam"]},
                 "page_size": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}}
        for extra in rng.sample(["start_date", "end_date", "status", "owner", "notes", "priority", "tags"], 3):
            props[extra] = {"type": "string", "description": f"Filter or field: {extra.replace('_', ' ')}"}
        server = d.replace("-", "")[:20]
        out.append(ToolRecord(
            qualified_name=f"{server}__{v}_{e}", server=server, upstream_name=f"{v}_{e}",
            description=(f"{v.capitalize()} a {d.replace('-', ' ')} {e}. Used by the {d} platform team to "
                         f"{v} {e}s for operational workflows; supports regional filtering and pagination."),
            input_schema={"type": "object", "properties": props, "required": [f"{e}_id"]},
        ))
    return out


def record_def(t: ToolRecord) -> dict:
    """Same shape as ``model_tool_def`` so Part A and Part B token counts are comparable."""
    return {"name": t.qualified_name, "description": t.description, "input_schema": t.input_schema}


def part_b(real_tools: list[ToolRecord], sizes: list[int]) -> list[dict]:
    rows = []
    real_avg = statistics.mean(tokens(record_def(t)) for t in real_tools)
    for size in sizes:
        catalog = real_tools + synthetic_tools(max(0, size - len(real_tools)))
        idx = ToolSearchIndex(catalog)
        defs_tokens = tokens([record_def(t) for t in catalog])
        r1 = r5 = total = 0
        for _, steps in TASKS:
            for query, expected in steps:
                names = [h.tool.qualified_name for h in idx.search(query, limit=5)]
                r1 += names[:1] == [expected]
                r5 += expected in names
                total += 1
        rows.append({"catalog_size": len(catalog), "full_definitions_tokens": defs_tokens,
                     "recall_at_1": round(r1 / total, 3), "recall_at_5": round(r5 / total, 3)})
    return rows, real_avg


def render_markdown(a: dict, b: list[dict], meta_tokens: int) -> str:
    tasks = a["tasks"]
    full_sum = sum(t["full_total"] for t in tasks)
    incr_sum = sum(t["incr_total"] for t in tasks)
    avg_disc = statistics.mean(t["discovery_tokens"] / t["tools_needed"] for t in tasks)
    lines = [
        "# Token usage: full vs incremental tool discovery",
        "",
        "Generated by `python -m experiments.token_usage` against a live gateway "
        f"({a['tool_count']} tools across 5 upstreams). Tokenizer: tiktoken `o200k_base` (provider-neutral proxy).",
        "",
        "## A. Live gateway, 20 tools",
        "",
        "| | tokens |",
        "|---|---:|",
        f"| All {a['tool_count']} tool definitions (`/mcp/full` → `tools/list`) | **{a['full_definitions_tokens']:,}** |",
        f"| 3 meta-tool definitions (`/mcp` → `tools/list`) | **{a['meta_definitions_tokens']:,}** |",
        f"| Avg. tokens per tool definition | {a['avg_tokens_per_tool']} |",
        f"| Avg. discovery cost per tool actually used (search + schema I/O) | {avg_disc:,.0f} |",
        "",
        "Per-request fixed overhead drops by "
        f"**{100 * (1 - a['meta_definitions_tokens'] / a['full_definitions_tokens']):.0f}%** "
        f"({a['full_definitions_tokens']:,} → {a['meta_definitions_tokens']:,}).",
        "",
        "### Per task (input tokens spent on tool definitions + discovery over the whole agent loop)",
        "",
        "| Task | tools used | full | incremental | Δ | search recall@1 | recall@5 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for t in tasks:
        delta = 100 * (t["incr_total"] - t["full_total"]) / t["full_total"]
        lines.append(f"| {t['task']} | {t['tools_needed']} | {t['full_total']:,} | {t['incr_total']:,} | "
                     f"{delta:+.0f}% | {t['recall_at_1']:.0%} | {t['recall_at_5']:.0%} |")
    lines += [
        f"| **Total (10 tasks)** | | **{full_sum:,}** | **{incr_sum:,}** | "
        f"**{100 * (incr_sum - full_sum) / full_sum:+.0f}%** | | |",
        "",
        "## B. Scaling the catalog (real 20 tools + synthetic distractors)",
        "",
        "Fixed per-request cost of exposing the whole catalog vs. the constant meta-tools, and whether search "
        "still finds the right tool among thousands of distractors (same 14 queries as above).",
        "",
        "| Catalog size | full definitions (tokens/request) | incremental (tokens/request) | reduction | recall@1 | recall@5 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in b:
        red = 100 * (1 - meta_tokens / row["full_definitions_tokens"])
        lines.append(f"| {row['catalog_size']:,} | {row['full_definitions_tokens']:,} | {meta_tokens:,} | "
                     f"{red:.2f}% | {row['recall_at_1']:.0%} | {row['recall_at_5']:.0%} |")
    lines += [
        "",
        "## Reading the results",
        "",
        "* **Fixed cost:** full discovery resends every definition on every model call, so it grows linearly "
        "with the catalog. Incremental discovery's fixed cost is constant (3 meta-tools).",
        "* **Variable cost:** incremental pays per tool *used* (one search + one schema fetch), plus extra "
        "round-trips. At 20 tools, single-tool tasks are already cheaper but multi-tool tasks can cost more, "
        "because the full catalog is small; the crossover moves sharply in incremental's favour as the "
        "catalog grows (Part B) — at hundreds of tools, full discovery is impractical and at thousands it "
        "exceeds typical context windows.",
        "* **Latency trade-off:** incremental adds 2 model round-trips per newly needed tool. Hybrid setups "
        "(pin a few hot tools, search the long tail) reduce this; the gateway supports direct calls by "
        "qualified name on `/mcp` for exactly that.",
        "* **Prompt caching** lowers the $ cost of a static tool prefix in full mode, but not the context "
        "window it occupies or the selection-accuracy cost of showing a model thousands of tools.",
        "* **Search quality** is the real risk of incremental discovery. Half of the synthetic distractors "
        "reuse the real tools' vocabulary (ride, fare, order, invoice…), so top-1 accuracy drops as they are "
        "added while top-5 holds — which is why `search_tools` returns several candidates and lets the model "
        "choose. The lexical BM25 ranker is a baseline; it is pluggable (`ToolSearchIndex.search`) for "
        "embedding/hybrid retrieval. Synthetic distractors are a proxy, not a substitute for a real catalog.",
    ]
    return "\n".join(lines) + "\n"


async def amain() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.add_argument("--key", default="demo-admin-key", help="a key that can see all tools")
    p.add_argument("--sizes", default="20,100,500,1000,5000")
    args = p.parse_args()

    a = await part_a(args.url, args.key)

    # Rebuild real ToolRecords from the live catalog for part B
    headers = {"Authorization": f"Bearer {args.key}"}
    import httpx
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=30) as c:
        catalog = (await c.get("/admin/tools", params={"limit": 5000})).json()["tools"]
    real = [ToolRecord(qualified_name=t["name"], server=t["name"].split("__")[0],
                       upstream_name=t["name"].split("__", 1)[1], description=t["description"],
                       input_schema=t["input_schema"], annotations=t.get("annotations")) for t in catalog]
    b, _ = part_b(real, [int(s) for s in args.sizes.split(",")])

    meta_tokens = a["meta_definitions_tokens"]
    (OUT_DIR / "results.json").write_text(json.dumps({"live": a, "scale": b}, indent=2), encoding="utf-8")
    md = render_markdown(a, b, meta_tokens)
    (OUT_DIR / "results.md").write_text(md, encoding="utf-8")
    print(md.encode("ascii", "replace").decode() if sys.stdout.encoding.lower() != "utf-8" else md)


if __name__ == "__main__":
    asyncio.run(amain())
