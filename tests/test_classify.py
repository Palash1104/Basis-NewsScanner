import pytest

from app.pipeline.classify import is_non_news, non_news_reason


@pytest.mark.parametrize(
    "title",
    [
        "What are all the sanctions Iran is under?",
        "Ghalibaf’s maths missile at Trump decoded: Is Iran fixing US interest rates?",
        "UPI MDR fee hike explained in 10 points: No rollback, Rahul Gandhi vs Centre",
        "In charts: How India’s Russian oil imports shifted amid US tariff flip-flops",
        "India-US tariff timeline: How Trump went from 10 to 100% duty over Russian oil",
        "Explainer: Why gold smuggling is booming and seizures are growing",
        "HT Evening Brief Sept 17: Tata Sons' U-turn on Chandrasekaran; hockey venue row",
        "India news Highlights, 16 September 2026: India voices concern over drone attack",
        "Why is inflation rising again around the world?",
    ],
)
def test_explainers_and_roundups_are_non_news(title: str) -> None:
    assert is_non_news(title), title


@pytest.mark.parametrize(
    "title",
    [
        "U.S. House passes Russia sanctions bill seeking to impose up to 100% tariffs on India",
        "Two arrested on charges of rape",
        "A 240-million-year-old fossil just changed the dinosaur timeline",
        "Who is Caden Qiao? Police seeking public's help to find missing Georgia Tech student",
        "Is Grand Central Terminal closed? Police activity impacts train services; one held",
        "Emami announces share buyback worth Rs 282 crore. Here's what you need to know",
        "Iran war has cost the US $38bn: How will it impact US economy, politics?",
        "Did the Clarity Act pass today? Here's what happened to the crypto bill",
    ],
)
def test_news_headlines_are_not_flagged(title: str) -> None:
    assert non_news_reason(title) is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Fed meeting live updates: Rate hike expected for the first time in three years",
        "‘So many loopholes’: Jeffries flags concerns over sanctions bill – US politics live",
        "Former Kosovo president Thaçi sentenced to 25 years in prison – as it happened",
        "DUSU Elections '26 LIVE: ABVP leading after Round 6",
        "Saudi Arabia LIVE updates: Houthis say they attacked Saudi's Aramco facilities in Yanbu",
        "Reader Q&A: how united are the Democrats ahead of the US midterms? Live now",
        "Chess Olympiad - Live!",
    ],
)
def test_live_blogs_are_non_news(title: str) -> None:
    reason = non_news_reason(title)
    assert reason is not None and reason.startswith("live blog"), title


@pytest.mark.parametrize(
    "title",
    [
        "Bus mishap, NBC chopper, fatal crash: The shocking turn of events during live coverage",
        "IND vs AFG live streaming today: How to watch the 3rd T20I live in India?",
        "Netflix content chief defines event strategy as streamer eyes more live sports",
        "Millions of people live in areas hit by the floods",
    ],
)
def test_the_word_live_alone_is_not_a_live_blog(title: str) -> None:
    assert not (non_news_reason(title) or "").startswith("live blog"), title
