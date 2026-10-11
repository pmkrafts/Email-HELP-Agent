"""Knowledge-base retrieval (offline RAG-lite).

Token-overlap scoring with IDF weighting over `data/knowledge_base/*.md`.
Deliberately simple and dependency-free; the seam (`retrieve`) takes top-k and returns
ContextChunk with citations, so an embedding-backed retriever can replace it later.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from .models import ContextChunk

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TOKEN = re.compile(r"[a-z][a-z'-]{2,}")
_STOP = {"the", "and", "for", "you", "your", "with", "that", "this", "have", "has", "are", "was", "were",
         "not", "but", "from", "they", "them", "will", "would", "could", "should", "about", "into", "over",
         "after", "before", "been", "being", "there", "their", "what", "when", "which", "while", "very"}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class KnowledgeBase:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.df: Counter[str] = Counter()
        for doc in docs:
            self.df.update(set(_tokens(doc["content"])))
        self.n = max(len(docs), 1)

    @classmethod
    def load(cls, directory: str | Path) -> "KnowledgeBase":
        docs: list[dict] = []
        root = Path(directory)
        if not root.is_dir():
            return cls([])
        for path in sorted(root.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            meta: dict[str, str] = {}
            match = _FRONTMATTER.match(raw)
            body = raw
            if match:
                body = raw[match.end():]
                for line in match.group(1).splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        meta[key.strip()] = value.strip()
            title = meta.get("title") or path.stem.replace("_", " ").title()
            docs.append({
                "source": meta.get("type", "document"),
                "doc_id": meta.get("id", path.stem),
                "title": title,
                "tags": meta.get("tags", ""),
                "content": body.strip(),
                "path": str(path),
            })
        return cls(docs)

    def retrieve(self, query: str, top_k: int = 3) -> list[ContextChunk]:
        if not self.docs:
            return []
        q_tokens = Counter(_tokens(query))
        if not q_tokens:
            return []
        scored: list[tuple[float, dict]] = []
        for doc in self.docs:
            d_tokens = Counter(_tokens(doc["content"]))
            score = 0.0
            for token, q_count in q_tokens.items():
                if token in d_tokens:
                    idf = math.log((self.n + 1) / (self.df[token] + 1)) + 1.0
                    score += min(q_count, d_tokens[token]) * idf
            tag_bonus = 0.15 * sum(1 for tag in re.split(r"[,\s]+", doc["tags"]) if tag and tag in query.lower())
            score += tag_bonus
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda pair: -pair[0])
        return [
            ContextChunk(source=doc["source"], title=doc["title"], content=doc["content"], score=round(score, 3))
            for score, doc in scored[:top_k]
        ]
