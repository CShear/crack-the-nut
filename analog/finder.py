"""KNN analog finder — the heart of the analog memory system.

Given a query fingerprint, finds the K most similar historical fingerprints.
Similarity is cosine distance with optional recency weighting. The result
is a weighted set of "analog periods" that can be scored for strategy
performance.

Critically, the finder enforces temporal causality: analogs can only come
from *before* the query timestamp. No lookahead.

Usage::

    finder = AnalogFinder(k=20, recency_halflife_days=180)

    # Load historical fingerprints
    finder.fit(store.load())

    # Find analogs for the current market state
    matches = finder.query(current_fingerprint)
    # matches[0].fingerprint, matches[0].similarity, matches[0].weight
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

from analog.fingerprint import Fingerprint

logger = structlog.get_logger()


@dataclass
class AnalogMatch:
    """A historical fingerprint matched as an analog."""

    fingerprint: Fingerprint
    similarity: float  # 0-1, higher = more similar
    recency_weight: float  # exponential decay, higher = more recent
    weight: float  # combined weight (similarity * recency)
    rank: int  # 1-based rank


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0-1."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


def _normalize_vector(v: list[float], means: list[float], stds: list[float]) -> list[float]:
    """Z-score normalize a vector given precomputed means and stds."""
    return [(x - m) / s if s > 0 else 0.0 for x, m, s in zip(v, means, stds)]


class AnalogFinder:
    """Find historical analogs by fingerprint similarity.

    Args:
        k: Number of analogs to return.
        recency_halflife_days: Half-life for recency weighting. An analog
            from `halflife` days ago gets 50% weight vs today. Set to 0
            to disable recency weighting.
        min_gap_hours: Minimum time gap between query and analog to avoid
            autocorrelation (e.g., the bar right before the query is too
            similar by construction, not by meaning).
        similarity_weight: How much similarity matters vs recency (0-1).
    """

    def __init__(
        self,
        k: int = 20,
        recency_halflife_days: float = 180.0,
        min_gap_hours: float = 8.0,
        similarity_weight: float = 0.7,
    ):
        self.k = k
        self.recency_halflife_seconds = recency_halflife_days * 86400
        self.min_gap_seconds = min_gap_hours * 3600
        self.similarity_weight = similarity_weight

        self._fingerprints: list[Fingerprint] = []
        self._feature_order: list[str] = []
        self._matrix: list[list[float]] = []
        self._timestamps: list[float] = []
        # Normalization stats
        self._means: list[float] = []
        self._stds: list[float] = []

    def fit(self, fingerprints: list[Fingerprint]) -> None:
        """Load historical fingerprints and precompute normalization stats.

        Call this once at startup, or when you've appended significant new data.
        """
        if not fingerprints:
            logger.warning("analog_finder_fit_empty")
            return

        # Discover feature order from first fingerprint
        self._feature_order = sorted(fingerprints[0].vector.keys())
        self._fingerprints = sorted(fingerprints, key=lambda fp: fp.timestamp)
        self._timestamps = [fp.timestamp for fp in self._fingerprints]

        # Build raw matrix
        raw_matrix = [fp.to_list(self._feature_order) for fp in self._fingerprints]

        # Compute means and stds for z-score normalization
        n_features = len(self._feature_order)
        n_samples = len(raw_matrix)
        self._means = [0.0] * n_features
        self._stds = [0.0] * n_features

        for j in range(n_features):
            col = [raw_matrix[i][j] for i in range(n_samples)]
            mean = sum(col) / n_samples
            var = sum((x - mean) ** 2 for x in col) / n_samples
            self._means[j] = mean
            self._stds[j] = math.sqrt(var)

        # Normalize
        self._matrix = [_normalize_vector(row, self._means, self._stds) for row in raw_matrix]

        logger.info(
            "analog_finder_fit",
            n_fingerprints=n_samples,
            n_features=n_features,
        )

    def query(
        self,
        fp: Fingerprint,
        k: int | None = None,
    ) -> list[AnalogMatch]:
        """Find the K most similar historical analogs for a query fingerprint.

        Only returns analogs from *before* the query timestamp (causal).
        """
        if not self._matrix:
            return []

        k = k or self.k

        # Normalize query vector using fitted stats
        raw_query = fp.to_list(self._feature_order)
        query_vec = _normalize_vector(raw_query, self._means, self._stds)

        query_ts = fp.timestamp
        min_ts = query_ts - self.min_gap_seconds

        # Score all candidates
        candidates: list[tuple[int, float, float, float]] = []  # (idx, sim, recency, combined)

        for i, (hist_vec, hist_ts) in enumerate(zip(self._matrix, self._timestamps)):
            # Enforce causality: analog must be before query, with gap
            if hist_ts > min_ts:
                continue

            sim = _cosine_similarity(query_vec, hist_vec)

            # Recency weight: exponential decay
            if self.recency_halflife_seconds > 0:
                age = query_ts - hist_ts
                recency = math.exp(-math.log(2) * age / self.recency_halflife_seconds)
            else:
                recency = 1.0

            # Combined weight
            combined = (self.similarity_weight * sim) + ((1 - self.similarity_weight) * recency)
            candidates.append((i, sim, recency, combined))

        # Sort by combined weight, take top K
        candidates.sort(key=lambda x: x[3], reverse=True)
        top_k = candidates[:k]

        matches = []
        for rank, (idx, sim, recency, combined) in enumerate(top_k, 1):
            matches.append(
                AnalogMatch(
                    fingerprint=self._fingerprints[idx],
                    similarity=round(sim, 4),
                    recency_weight=round(recency, 4),
                    weight=round(combined, 4),
                    rank=rank,
                )
            )

        return matches

    @property
    def feature_order(self) -> list[str]:
        """The feature ordering used for distance computation."""
        return self._feature_order

    @property
    def size(self) -> int:
        """Number of fingerprints in the index."""
        return len(self._fingerprints)

    def analog_quality(self, matches: list[AnalogMatch]) -> dict[str, float]:
        """Assess the quality of an analog set.

        Returns metrics that indicate whether the system should trust
        its analog-based recommendations or sit out (0x risk).

        Keys:
            mean_similarity: Average similarity of top-K. Low = novel market.
            similarity_spread: Std dev of similarities. High = inconsistent matches.
            time_span_days: How spread out the analogs are in time.
            confidence: 0-1 composite confidence in this analog set.
        """
        if not matches:
            return {"mean_similarity": 0.0, "similarity_spread": 0.0, "time_span_days": 0.0, "confidence": 0.0}

        sims = [m.similarity for m in matches]
        mean_sim = sum(sims) / len(sims)
        var_sim = sum((s - mean_sim) ** 2 for s in sims) / len(sims)
        spread = math.sqrt(var_sim)

        timestamps = [m.fingerprint.timestamp for m in matches]
        time_span = (max(timestamps) - min(timestamps)) / 86400 if len(timestamps) > 1 else 0.0

        # Confidence: high similarity + low spread + decent time diversity
        sim_score = min(mean_sim / 0.9, 1.0)  # 0.9 similarity → full score
        spread_penalty = max(0.0, 1.0 - spread * 5)  # penalize high spread
        diversity_bonus = min(time_span / 90, 1.0)  # reward analogs spanning 90+ days
        confidence = (sim_score * 0.5 + spread_penalty * 0.3 + diversity_bonus * 0.2)

        return {
            "mean_similarity": round(mean_sim, 4),
            "similarity_spread": round(spread, 4),
            "time_span_days": round(time_span, 1),
            "confidence": round(confidence, 4),
        }
