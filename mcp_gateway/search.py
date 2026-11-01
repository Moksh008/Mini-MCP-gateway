"""Lexical tool search (BM25F-style) used for incremental tool discovery.

Each tool is indexed from several fields with different weights (name > summary/tags >
description > parameter names). Queries are tokenized the same way, with light stemming and
a small domain-agnostic synonym table. The index is rebuilt whenever the registry catalog
version changes; at a few thousand tools rebuilding takes milliseconds.

The ranking function is behind :class:`ToolSearchIndex.search`, so an embedding-based or
hybrid ranker can be dropped in without touching the gateway.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .models import ToolRecord

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it its me my of on or our please that the "
    "this to was we were will with you your can could would should do does get give want need".split()
)

# Query-side expansion: maps a token to related tokens (added at reduced weight).
SYNONYMS: dict[str, tuple[str, ...]] = {
    "cost": ("price", "fare", "estimate"),
    "price": ("cost", "fare", "estimate"),
    "fare": ("price", "cost"),
    "book": ("request", "order", "create"),
    "order": ("place", "book"),
    "cancel": ("cancellation", "delete"),
    "status": ("track", "state", "progress"),
    "track": ("status",),
    "where": ("track", "status", "location"),
    "address": ("geocode", "location"),
    "coordinates": ("geocode", "latitude", "longitude"),
    "directions": ("route",),
    "time": ("eta", "duration"),
    "eta": ("time", "arrival"),
    "refund": ("reverse", "return"),
    "money": ("payment", "refund", "charge"),
    "pay": ("charge", "payment"),
    "charge": ("pay", "payment"),
    "card": ("payment", "method"),
    "receipt": ("invoice",),
    "invoice": ("receipt", "bill"),
    "complaint": ("ticket", "support", "issue"),
    "help": ("support", "ticket"),
    "issue": ("ticket", "problem"),
    "food": ("restaurant", "menu", "eat"),
    "restaurant": ("food", "menu"),
    "trip": ("ride",),
    "ride": ("trip",),
    "car": ("ride",),
    "taxi": ("ride",),
    "history": ("list", "past"),
    "find": ("search", "list"),
    "lookup": ("get", "search"),
    "escalate": ("urgent", "priority"),
}

FIELD_WEIGHTS = {"name": 3.0, "summary": 1.5, "tags": 1.5, "server": 1.0, "description": 1.0, "params": 0.75}


def _stem(tok: str) -> str:
    if tok.endswith(("ss", "us", "is")):  # status, bus, analysis
        return tok
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(tok) > len(suffix) + 2 and tok.endswith(suffix):
            return tok[: -len(suffix)] + repl
    return tok


def tokenize(text: str) -> list[str]:
    text = _CAMEL_RE.sub(" ", text).replace("_", " ").replace("-", " ").lower()
    return [_stem(t) for t in _TOKEN_RE.findall(text) if t not in STOPWORDS]


@dataclass(frozen=True)
class SearchHit:
    tool: ToolRecord
    score: float


class ToolSearchIndex:
    def __init__(self, tools: list[ToolRecord], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._tools = tools
        self._doc_tf: list[Counter[str]] = []
        self._doc_len: list[float] = []
        df: Counter[str] = Counter()
        for tool in tools:
            tf: Counter[str] = Counter()
            for field, weight in FIELD_WEIGHTS.items():
                for tok in tokenize(self._field_text(tool, field)):
                    tf[tok] += weight
            self._doc_tf.append(tf)
            self._doc_len.append(sum(tf.values()))
            df.update(tf.keys())
        n = max(1, len(tools))
        self._avg_len = (sum(self._doc_len) / n) if tools else 1.0
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._postings: dict[str, list[int]] = defaultdict(list)
        for i, tf in enumerate(self._doc_tf):
            for tok in tf:
                self._postings[tok].append(i)

    @staticmethod
    def _field_text(tool: ToolRecord, field: str) -> str:
        if field == "name":
            return f"{tool.upstream_name} {tool.title or ''}"
        if field == "summary":
            return tool.summary()
        if field == "tags":
            return " ".join(tool.tags)
        if field == "server":
            return tool.server
        if field == "description":
            return tool.description
        props = (tool.input_schema or {}).get("properties", {}) or {}
        return " ".join(props.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def search(
        self, query: str, *, limit: int = 5, allowed: set[str] | None = None, server: str | None = None,
    ) -> list[SearchHit]:
        q_weights: dict[str, float] = defaultdict(float)
        for tok in tokenize(query):
            q_weights[tok] = max(q_weights[tok], 1.0)
            for syn in SYNONYMS.get(tok, ()):
                s = _stem(syn)
                q_weights[s] = max(q_weights[s], 0.4)
        # Exact qualified-name lookups should always rank first.
        exact = query.strip()
        scores: dict[int, float] = defaultdict(float)
        for tok, qw in q_weights.items():
            idf = self._idf.get(tok)
            if idf is None:
                continue
            for i in self._postings[tok]:
                tf = self._doc_tf[i][tok]
                norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self._doc_len[i] / self._avg_len))
                scores[i] += qw * idf * norm
        for i, t in enumerate(self._tools):
            if t.qualified_name == exact or t.upstream_name == exact:
                scores[i] += 100.0
        hits = []
        for i, score in scores.items():
            tool = self._tools[i]
            if allowed is not None and tool.qualified_name not in allowed:
                continue
            if server and tool.server != server:
                continue
            hits.append(SearchHit(tool, round(score, 4)))
        hits.sort(key=lambda h: (-h.score, h.tool.qualified_name))
        return hits[:limit]
