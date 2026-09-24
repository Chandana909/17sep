"""Agent memory: episodic, semantic, procedural, pattern and outcome memory, plus a vector
index for similarity. Retrieval results are leads for the agent, never evidence: the tool
that exposes similarity is flagged lead-only and its output cannot enter verification.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict

from asas.agents.tools import ToolOutputList
from asas.core.ids import content_hash
from asas.domain.models import InvestigationResult, Pattern
from asas.engine.hypotheses import CATALOG_VERSION
from asas.engine.snapshot import Snapshot

_DIMS = 1 << 12


def _tokens(snap: Snapshot, episode_id: str) -> list[str]:
    s = snap.signal_set.signals[episode_id]
    sig = s.labels["sequence_signature"]
    tokens = [f"sig:{sig}", f"desk:{s.labels['desk']}", f"product:{s.labels['product_type']}"]
    tokens += [f"step:{t}" for t in sig.split(">")]
    tokens += [f"flag:{k}" for k, v in sorted(s.flags.items()) if v]
    tokens += [f"rule:{r}" for r in snap.episode_rules(episode_id)]
    return tokens


def _hash(token: str) -> int:
    return int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % _DIMS


class VectorIndex:
    """Hashed TF-IDF vectors with cosine similarity (deterministic, dependency-free)."""

    def __init__(self, docs: Mapping[str, list[str]]) -> None:
        df: Counter[int] = Counter()
        for tokens in docs.values():
            df.update({_hash(t) for t in tokens})
        n = max(len(docs), 1)
        self._idf = {h: math.log((1 + n) / (1 + c)) + 1 for h, c in df.items()}
        self._vectors: dict[str, dict[int, float]] = {}
        for key, tokens in docs.items():
            tf = Counter(_hash(t) for t in tokens)
            vec = {h: c * self._idf[h] for h, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._vectors[key] = {h: v / norm for h, v in vec.items()}
        self.version = content_hash(
            sorted((k, sorted(v.items())) for k, v in self._vectors.items())
        )[:16]

    def similar(self, key: str, k: int, exclude: Iterable[str] = ()) -> list[tuple[str, float]]:
        query = self._vectors.get(key)
        if query is None:
            return []
        skip = set(exclude) | {key}
        scored = []
        for other, vec in self._vectors.items():
            if other in skip:
                continue
            score = sum(w * vec.get(h, 0.0) for h, w in query.items())
            scored.append((other, round(score, 4)))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]


class EntityProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entity: str
    desk: str
    episodes: int
    top_signatures: tuple[str, ...]


class Memory:
    """Read-only memory view for one snapshot (writes happen in the platform service)."""

    def __init__(
        self,
        snap: Snapshot,
        investigations: Mapping[str, InvestigationResult],
        patterns: Iterable[Pattern],
    ) -> None:
        self.snapshot = snap
        self.episodic = dict(investigations)
        self.patterns = list(patterns)
        prior = [e.episode_id for e in snap.episodes]
        self.index = VectorIndex({eid: _tokens(snap, eid) for eid in prior})
        self.semantic = self._profiles()
        self.procedural_version = CATALOG_VERSION

    def _profiles(self) -> dict[str, EntityProfile]:
        per_book: dict[str, list[str]] = defaultdict(list)
        desk_of: dict[str, str] = {}
        for ep in self.snapshot.episodes:
            s = self.snapshot.signal_set.signals[ep.episode_id]
            per_book[s.labels["book"]].append(s.labels["sequence_signature"])
            desk_of[s.labels["book"]] = s.labels["desk"]
        return {
            book: EntityProfile(
                entity=f"book:{book}",
                desk=desk_of[book],
                episodes=len(sigs),
                top_signatures=tuple(sig for sig, _ in Counter(sigs).most_common(3)),
            )
            for book, sigs in sorted(per_book.items())
        }

    @property
    def version(self) -> str:
        return content_hash(
            [
                self.index.version,
                sorted(self.episodic),
                self.procedural_version,
                [p.pattern_id for p in self.patterns],
            ]
        )[:16]

    def similar_cases(self, episode_id: str, k: int) -> ToolOutputList:
        ep = self.snapshot.episodes_by_id[episode_id]
        earlier = [e.episode_id for e in self.snapshot.episodes if e.end >= ep.start]
        items = []
        for other, score in self.index.similar(episode_id, k, exclude=earlier):
            past = self.episodic.get(other)
            items.append(
                {
                    "episode_id": other,
                    "similarity": str(score),
                    "signature": self.snapshot.signal_set.signals[other].labels[
                        "sequence_signature"
                    ],
                    "past_conclusion": (past.conclusion or "ABSTAINED")
                    if past
                    else "not investigated",
                }
            )
        return ToolOutputList(
            items=tuple(items),
            lead_only=True,
            note="similarity is a lead for what to check, never evidence",
        )
