"""Group the scanner's articles and filings into stories, across stocks.

The pipeline's rules (cosine similarity to a story's centroid, and to its seed so a story
can't drift into a blob), with one addition: an item may only join a story that shares one of
its stocks. So "Russia partners Adani Defence, HAL, BDL..." is one story touching HAL and
BDL - one alert later, not two - while an HAL order and a BDL order on the same day, worded
alike, stay two stories.
"""

from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np


@dataclass
class _Story:
    symbols: set[str]
    total: np.ndarray
    centroid: np.ndarray
    seed: np.ndarray
    last: datetime
    members: int = 1


@dataclass
class StoryGrouper[K: Hashable]:
    threshold: float
    seed_threshold: float | None
    window: timedelta
    _stories: dict[K, _Story] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._stories)

    def match(self, vector: np.ndarray, symbols: Iterable[str], seen_at: datetime) -> K | None:
        """The most similar story sharing a stock that clears both thresholds, or None."""
        wanted = set(symbols)
        best: K | None = None
        best_score = -1.0
        for key, story in self._stories.items():
            if not story.symbols & wanted or story.last < seen_at - self.window:
                continue
            score = float(story.centroid @ vector)
            if score < self.threshold or score <= best_score:
                continue
            if self.seed_threshold is not None and float(story.seed @ vector) < self.seed_threshold:
                continue
            best, best_score = key, score
        return best

    def add(self, key: K, vector: np.ndarray, symbols: Iterable[str], seen_at: datetime) -> None:
        """Add an item to story `key`, creating it (with this item as its seed) if needed.
        Callers add items oldest first, as the pipeline does."""
        story = self._stories.get(key)
        if story is None:
            self._stories[key] = _Story(
                set(symbols), vector.copy(), vector.copy(), vector.copy(), seen_at
            )
            return
        story.symbols |= set(symbols)
        story.total = story.total + vector
        norm = float(np.linalg.norm(story.total))
        story.centroid = story.total / norm if norm else story.total
        story.last = max(story.last, seen_at)
        story.members += 1
