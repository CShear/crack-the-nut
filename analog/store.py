"""Append-only fingerprint store backed by Parquet files.

Stores historical fingerprints as columnar data for fast KNN queries.
Each fingerprint is one row. The store auto-manages partitioning by month
for efficient range queries during backfill.

Usage::

    store = FingerprintStore("data/fingerprints")
    store.append(fingerprint)
    store.append_batch(fingerprints)

    # Load all fingerprints (for KNN search)
    all_fps = store.load()

    # Load a time range
    recent = store.load(start_ts=time.time() - 86400 * 90)

    # Get feature matrix for distance computation
    matrix, timestamps = store.as_matrix(feature_order)
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment]

from analog.fingerprint import Fingerprint  # noqa: E402


class FingerprintStore:
    """Parquet-backed append-only store for market fingerprints.

    Args:
        data_dir: Directory for Parquet files. Created if missing.
    """

    def __init__(self, data_dir: str = "data/fingerprints"):
        if pd is None:
            raise ImportError("pandas is required for FingerprintStore")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._buffer: list[dict[str, float | str]] = []

    def _partition_key(self, ts: float) -> str:
        """Monthly partition key from unix timestamp."""
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m")

    def _partition_path(self, key: str) -> Path:
        return self.data_dir / f"fingerprints_{key}.parquet"

    def append(self, fp: Fingerprint) -> None:
        """Append a single fingerprint. Writes to buffer, flush to persist."""
        self._buffer.append(fp.to_dict())

    def append_batch(self, fps: list[Fingerprint]) -> None:
        """Append multiple fingerprints."""
        self._buffer.extend(fp.to_dict() for fp in fps)

    def flush(self) -> int:
        """Write buffered fingerprints to Parquet files. Returns count written."""
        if not self._buffer:
            return 0

        df = pd.DataFrame(self._buffer)
        count = len(df)

        # Partition by month
        df["_month"] = df["timestamp"].apply(
            lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
        )

        for month, group in df.groupby("_month"):
            group = group.drop(columns=["_month"])
            path = self._partition_path(month)

            if path.exists():
                existing = pd.read_parquet(path)
                combined = pd.concat([existing, group], ignore_index=True)
                combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
                combined = combined.sort_values("timestamp").reset_index(drop=True)
                combined.to_parquet(path, index=False)
            else:
                group = group.sort_values("timestamp").reset_index(drop=True)
                group.to_parquet(path, index=False)

        self._buffer.clear()
        logger.info("fingerprints_flushed", count=count)
        return count

    def load(
        self,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[Fingerprint]:
        """Load fingerprints from Parquet, optionally filtered by time range."""
        parts = sorted(self.data_dir.glob("fingerprints_*.parquet"))
        if not parts:
            return []

        frames = []
        for p in parts:
            df = pd.read_parquet(p)
            if start_ts is not None:
                df = df[df["timestamp"] >= start_ts]
            if end_ts is not None:
                df = df[df["timestamp"] <= end_ts]
            if not df.empty:
                frames.append(df)

        if not frames:
            return []

        combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
        fingerprints = []
        for _, row in combined.iterrows():
            ts = row["timestamp"]
            vector = {k: float(v) for k, v in row.items() if k != "timestamp"}
            fingerprints.append(Fingerprint(timestamp=ts, vector=vector))

        return fingerprints

    def as_matrix(
        self,
        feature_order: list[str],
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> tuple[list[list[float]], list[float]]:
        """Load fingerprints as a feature matrix + timestamp array.

        Returns:
            (matrix, timestamps) where matrix[i] is the feature vector for
            timestamps[i], in the order specified by feature_order.
        """
        fps = self.load(start_ts=start_ts, end_ts=end_ts)
        timestamps = [fp.timestamp for fp in fps]
        matrix = [fp.to_list(feature_order) for fp in fps]
        return matrix, timestamps

    def count(self) -> int:
        """Total fingerprints stored (excluding buffer)."""
        total = 0
        for p in self.data_dir.glob("fingerprints_*.parquet"):
            df = pd.read_parquet(p, columns=["timestamp"])
            total += len(df)
        return total

    @property
    def buffered(self) -> int:
        """Number of fingerprints in the write buffer."""
        return len(self._buffer)
