"""Group the scanner's articles and filings into stories, across stocks.

The pipeline's rules (cosine similarity to a story's centroid, and to its seed so a story
can't drift into a blob), with one addition: an item may only join a story that shares one of
its stocks. So "Russia partners Adani Defence, HAL, BDL..." is one story touching HAL and
BDL - one alert later, not two - while an HAL order and a BDL order on the same day, worded
alike, stay two stories. Two items stating different money amounts are two events
(app/watch/events.py) - "BDL's Rs 56.8 crore tender" is not "Rs 810 crore contract with BDL",
however alike the embedding model finds them. And a story begun by share-price commentary
takes no event: Zen's Cogknit acquisition is not more of "Share Price Rises 5.39% on
Valuation View".
"""

from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from app.watch.events import Amount, amounts_conflict


@dataclass
class _Story:
    symbols: set[str]
    total: np.ndarray
    centroid: np.ndarray
    seed: np.ndarray
    last: datetime
    members: int = 1
    amounts: set[Amount] = field(default_factory=set)
    commentary: bool = False  # begun by share-price commentary: closed to events


@dataclass
class StoryGrouper[K: Hashable]:
    threshold: float
    seed_threshold: float | None
    window: timedelta
    _stories: dict[K, _Story] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._stories)

    def match(
        self,
        vector: np.ndarray,
        symbols: Iterable[str],
        seen_at: datetime,
        amounts: Iterable[Amount] = (),
        event: bool = False,
    ) -> K | None:
        """The most similar story sharing a stock that clears both thresholds, states no
        money amount that disagrees with this item's and, for an event, didn't begin as
        share-price commentary; or None."""
        wanted = set(symbols)
        stated = set(amounts)
        best: K | None = None
        best_score = -1.0
        for key, story in self._stories.items():
            if not story.symbols & wanted or story.last < seen_at - self.window:
                continue
            if amounts_conflict(story.amounts, stated) or (event and story.commentary):
                continue
            score = float(story.centroid @ vector)
            if score < self.threshold or score <= best_score:
                continue
            if self.seed_threshold is not None and float(story.seed @ vector) < self.seed_threshold:
                continue
            best, best_score = key, score
        return best

    def add(
        self,
        key: K,
        vector: np.ndarray,
        symbols: Iterable[str],
        seen_at: datetime,
        amounts: Iterable[Amount] = (),
        commentary: bool = False,
    ) -> None:
        """Add an item to story `key`, creating it (with this item as its seed) if needed.
        Callers add items oldest first, as the pipeline does. `commentary`: whether the story
        began as share-price commentary - read when the story is created, ignored after."""
        story = self._stories.get(key)
        if story is None:
            self._stories[key] = _Story(
                set(symbols),
                vector.copy(),
                vector.copy(),
                vector.copy(),
                seen_at,
                amounts=set(amounts),
                commentary=commentary,
            )
            return
        story.amounts |= set(amounts)
        story.symbols |= set(symbols)
        story.total = story.total + vector
        norm = float(np.linalg.norm(story.total))
        story.centroid = story.total / norm if norm else story.total
        story.last = max(story.last, seen_at)
        story.members += 1
