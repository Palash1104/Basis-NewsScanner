"""Flag articles that aren't news reports: explainers, backgrounders, daily roundups and live
blogs.

Non-news articles can attach to an existing story, but they never start a story, never move a
story's centroid, never count as an independent source, and aren't sent to the summarizer or
shown as digest sources. The rules only look at the headline, and are deliberately narrow:
a headline that also reports something ("Who is X? Police seek help finding…") stays news.
"""

import re

# Headline words that mark explainers and backgrounders.
_EXPLAINER = re.compile(
    r"\b(explained|explainer|decoded|in charts|in numbers|fact[- ]check|faqs?|"
    r"everything you need to know|all you need to know)\b"
    r"|\btimeline\s*[:|]|^timeline\b|\btimeline of\b",
    re.IGNORECASE,
)
# Daily roundups and newsletters that bundle several unrelated stories.
_ROUNDUP = re.compile(
    r"\b(morning|evening|daily|weekly) brief\b|\bnews (highlights|wrap|roundup|round-up)\b",
    re.IGNORECASE,
)
# Live blogs: rolling pages that mix many developments, like roundups. Plain "live" in a
# sentence ("where millions live") doesn't count; only live-blog labels do.
_LIVE_BLOG = re.compile(
    r"\blive[- ]?(updates?|blog|scores?)\b|\bas it happened\b|\blive now\b"
    r"|\blive\s*[:|]|[:|–-]\s*live\b|\blive$",
    re.IGNORECASE,
)
_LIVE_CAPS = re.compile(r"\bLIVE\b")  # "DUSU Elections '26 LIVE: ...", "Saudi Arabia LIVE updates"
# A headline led by a question ("What are all the sanctions Iran is under?")...
_QUESTION_LEAD = re.compile(
    r"^(what|what's|who|why|how|which|is|are|can|will|should|does|do)\b[^:|?]*\?",
    re.IGNORECASE,
)
# ...counts as an explainer only if nothing newsy follows the question mark.
_QUESTION_TAIL = re.compile(
    r"^(explained|answered|here'?s (why|what|how)|what we know|all about\b.*)?[.!]?$",
    re.IGNORECASE,
)


def non_news_reason(title: str) -> str | None:
    """Why the headline looks like an explainer or roundup, or None if it reads as news."""
    text = " ".join(title.replace("’", "'").split())
    if match := _ROUNDUP.search(text):
        return f"roundup ({match.group(0).lower()})"
    if match := (_LIVE_BLOG.search(text) or _LIVE_CAPS.search(text)):
        return f"live blog ({match.group(0).lower().strip(' :|–-')})"
    if match := _EXPLAINER.search(text):
        return f"explainer ({match.group(0).lower().strip(' :|')})"
    if match := _QUESTION_LEAD.match(text):
        tail = text[match.end() :].strip(" -–|:")
        if _QUESTION_TAIL.match(tail):
            return "explainer (question headline)"
    return None


def is_non_news(title: str) -> bool:
    return non_news_reason(title) is not None
