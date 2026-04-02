"""Analog Memory Trading — similarity-based strategy selection.

Instead of classifying markets into discrete regime labels, this package
finds the historical periods most similar to "right now" and asks what
strategies would have worked in those analog periods.

Modules:
    fingerprint — compute a dense market-state vector every interval
    surface     — funding rate surface across assets (mean, dispersion, skew)
    store       — append-only Parquet store for historical fingerprints
    finder      — KNN analog search with weighted similarity
    scorer      — score strategy performance across an analog set
"""

from analog.fingerprint import Fingerprint, FingerprintEngine
from analog.surface import FundingSurface, FundingSurfaceEngine
from analog.store import FingerprintStore
from analog.finder import AnalogFinder, AnalogMatch
from analog.scorer import AnalogScorer, StrategyScore

__all__ = [
    "Fingerprint",
    "FingerprintEngine",
    "FundingSurface",
    "FundingSurfaceEngine",
    "FingerprintStore",
    "AnalogFinder",
    "AnalogMatch",
    "AnalogScorer",
    "StrategyScore",
]
