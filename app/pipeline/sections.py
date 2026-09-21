"""What section of an outlet an article came from, read from its URL.

Used only to protect the reserved slots (SPEC 7.4): a slot kept for Indian news must not go
to an Asian Games final when the LLM rerank is unavailable and the computed importance order
is all we have.

Sections are read as whole path segments, never as substrings of a headline slug, because
"sport" is inside pas**sport**, "ipl" inside di**pl**omats and "celeb" inside **celeb**rate.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.models import Story

# Sections we would never spend a reserved slot on.
SOFT_SECTIONS = frozenset(
    {
        # sport
        "sport",
        "sports",
        "other-sports",
        "cricket",
        "football",
        "tennis",
        "hockey",
        "badminton",
        "kabaddi",
        "olympics",
        "asian-games",
        "ipl",
        "f1",
        "motorsport",
        "wwe",
        "chess",
        # entertainment
        "entertainment",
        "bollywood",
        "hollywood",
        "movies",
        "movie",
        "film",
        "films",
        "television",
        "tv",
        "web-series",
        "music",
        "celebrity",
        "celebrities",
        "celebs",
        "art",
        "books",
        "culture",
        # lifestyle
        "lifestyle",
        "life-style",
        "fashion",
        "beauty",
        "food",
        "recipes",
        "travel",
        "fitness",
        "health-fitness",
        "relationships",
        "astrology",
        "horoscope",
        "spirituality",
        "religion",
        # filler formats
        "photos",
        "videos",
        "gallery",
        "web-stories",
        "trending",
        "viral",
        "trends",
        "quiz",
        "games",
        "gaming",
        "shopping",
        "deals",
    }
)
# Path segments that name no section of their own.
_WRAPPERS = frozenset(
    {"article", "articleshow", "articles", "news", "rss", "amp", "story", "en", "feeder"}
)
# A section is a short word or two; anything longer is the headline slug.
_SECTION = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_MAX_SECTIONS = 2


def url_sections(url: str) -> list[str]:
    """The section segments of a URL, or an empty list when it says nothing.

    Google News delivers redirects whose path is an opaque blob, so roughly a tenth of Indian
    articles have no readable section; the caller decides what "unknown" means.
    """
    split = urlsplit(url)
    if split.netloc.endswith("news.google.com"):
        return []
    found: list[str] = []
    for part in (segment.lower() for segment in split.path.split("/") if segment):
        if part in _WRAPPERS:
            continue
        if not _SECTION.match(part) or part.count("-") > 2 or "." in part:
            break  # the headline slug starts here
        found.append(part)
        if len(found) == _MAX_SECTIONS:
            break
    return found


def is_soft_section(url: str) -> bool:
    """Whether the URL's section marks sport, entertainment, lifestyle or filler."""
    return any(section in SOFT_SECTIONS for section in url_sections(url))


def may_take_reserved_slot(story: Story) -> bool:
    """Whether a story may fill a slot reserved for its region.

    It must have at least one news article *known* to be hard news: a readable section that
    isn't soft. Unknown counts as no, because slots are few and candidates are many - on a
    normal day hundreds of Indian stories compete for five - so waiting for a run where the
    rerank works costs nothing, while one Asian Games final in the digest costs a slot.
    """
    articles = [article for article in story.articles if not article.non_news] or story.articles
    return any(url_sections(a.url) and not is_soft_section(a.url) for a in articles)


def fresh_enough(story: Story, now: datetime, max_age_hours: int | None) -> bool:
    """Whether a story is young enough for a reserved slot, measured from `first_seen_at`.

    Hundreds of single-region stories wait unsummarized, some for days. Without a cap the
    reserve would work through the backlog instead of covering what is happening now.
    """
    if max_age_hours is None:
        return True
    return story.first_seen_at >= now - timedelta(hours=max_age_hours)


def source_regions(story: Story) -> set[str]:
    """The regions of the feeds a story came from (not the regions its summary is about)."""
    news = {a.source_region for a in story.articles if not a.non_news}
    return news or {a.source_region for a in story.articles}


def only_from(story: Story, region: str) -> bool:
    return source_regions(story) == {region}


def regional_candidates(
    stories: Sequence[Story],
    region: str,
    limit: int,
    now: datetime | None = None,
    max_age_hours: int | None = None,
) -> list[Story]:
    """The best stories carried only by that region's outlets, most important first."""
    single = [story for story in stories if only_from(story, region)]
    if now is not None:
        single = [story for story in single if fresh_enough(story, now, max_age_hours)]
    single.sort(key=lambda story: story.importance_score, reverse=True)
    return single[:limit]
