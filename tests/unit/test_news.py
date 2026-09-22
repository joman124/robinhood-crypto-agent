"""News: feeds, Jev's labels, and how a headline enters the composite."""

from datetime import datetime, timedelta, timezone

import pytest

from robinhood_crypto_agent.config import StrategyConfig
from robinhood_crypto_agent.errors import AgentError
from robinhood_crypto_agent.jev import JevClient, labels_from_answers, news_questions
from robinhood_crypto_agent.models import NewsItem, NewsLabels, utcnow
from robinhood_crypto_agent.news import NewsStore, parse_feed
from robinhood_crypto_agent.strategy import CompositeStrategy
from robinhood_crypto_agent.strategy.base import SignalContext
from robinhood_crypto_agent.strategy.signals import (
    BreakoutSignal,
    MeanReversionSignal,
    MomentumSignal,
    NewsSignal,
    TrendSignal,
)
from tests.conftest import make_candles, uptrend

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
<item>
  <title>Bitcoin &amp; ETFs: record &lt;b&gt;inflows&lt;/b&gt;</title>
  <link>https://example.com/a</link>
  <guid>a-1</guid>
  <pubDate>Mon, 21 Sep 2026 03:10:00 +0000</pubDate>
  <description><![CDATA[<p>A <b>big</b> day</p>]]></description>
</item>
<item><title>Undated, so unplaceable</title><link>https://example.com/b</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Solana validators halt</title>
    <link href="https://example.com/s"/>
    <id>tag:example.com,2026:s</id>
    <updated>2026-09-21T02:00:00Z</updated>
    <summary>Block production stopped</summary>
  </entry>
</feed>"""


def labeled(direction="bullish", *, impact=3.0, asset="BTC", minutes_ago=10, confidence=0.9):
    return NewsItem(
        item_id=f"{direction}-{impact}-{minutes_ago}",
        source="example.com",
        title=f"{direction} headline",
        published_at=utcnow() - timedelta(minutes=minutes_ago),
        labels=NewsLabels(asset, confidence, direction, confidence, impact),
    )


class TestFeeds:
    def test_rss_items_are_parsed_and_cleaned(self):
        [item] = parse_feed(RSS, source="example.com")  # the undated item is dropped
        assert item.title == "Bitcoin & ETFs: record inflows"
        assert item.summary == "A big day"
        assert item.url == "https://example.com/a"
        assert item.published_at == datetime(2026, 9, 21, 3, 10, tzinfo=timezone.utc)

    def test_atom_entries_are_parsed(self):
        [item] = parse_feed(ATOM, source="example.com")
        assert item.title == "Solana validators halt"
        assert item.url == "https://example.com/s"
        assert item.published_at == datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)

    def test_ids_are_stable_and_scoped_to_the_source(self):
        first = parse_feed(RSS, source="example.com")[0].item_id
        assert parse_feed(RSS, source="example.com")[0].item_id == first
        assert parse_feed(RSS, source="elsewhere.com")[0].item_id != first

    def test_a_broken_feed_is_an_error_not_a_crash(self):
        with pytest.raises(AgentError, match="not a valid RSS/Atom"):
            parse_feed(b"<rss><channel>", source="example.com")

    def test_a_feed_declaring_entities_is_refused(self):
        """Entity expansion turns a kilobyte of XML into gigabytes of memory."""
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>'
        with pytest.raises(AgentError, match="entities"):
            parse_feed(bomb, source="example.com")


class TestStore:
    def test_labeled_items_round_trip(self, tmp_path):
        store = NewsStore(tmp_path / "news.jsonl")
        item = labeled()
        store.append([item])
        assert store.items() == [item]
        assert item.item_id in store.seen_ids()


class TestJevLabels:
    ANSWERS = {
        "model": "jev-1.13.0",
        "answers": {
            "asset": {"type": "choice", "choice": "BTC", "confidence": 0.9},
            "direction": {"type": "choice", "choice": "bearish", "confidence": 0.8},
            "impact": {"type": "score", "score": 2.4, "confidence": 0.6},
        },
    }

    def test_the_questions_offer_the_watchlist_the_market_and_other(self):
        questions = news_questions(["BTC-USD", "ETH-USD"])
        assert set(questions["asset"]["criteria"]) == {"BTC", "ETH", "MARKET", "OTHER"}
        assert len(questions["impact"]["criteria"]) == 4

    def test_answers_become_labels(self):
        labels = labels_from_answers(self.ANSWERS, allowed_assets=["BTC", "MARKET", "OTHER"])
        assert (labels.asset, labels.direction, labels.impact) == ("BTC", "bearish", 2.4)
        assert labels.model == "jev-1.13.0"

    def test_an_answer_outside_the_options_is_refused(self):
        with pytest.raises(AgentError, match="not one of the options"):
            labels_from_answers(self.ANSWERS, allowed_assets=["ETH", "MARKET", "OTHER"])

    def test_a_confidence_outside_zero_to_one_is_refused(self):
        bad = {
            **self.ANSWERS,
            "answers": {
                **self.ANSWERS["answers"],
                "direction": {"choice": "bullish", "confidence": 1.5},
            },
        }
        with pytest.raises(AgentError, match="outside"):
            labels_from_answers(bad, allowed_assets=["BTC", "MARKET", "OTHER"])

    def test_the_client_sends_the_headline_and_questions(self):
        sent = {}

        def http(method, url, *, headers=None, body=None, timeout=None):
            sent.update(method=method, url=url, headers=headers, body=body)
            return self.ANSWERS

        item = NewsItem("i1", "example.com", "Exchange hacked", utcnow(), summary="Funds gone")
        JevClient("jev-key", http=http).label(item, ["BTC-USD"])
        assert sent["url"] == "https://api.typesafe.ai/v1/systemone"
        assert sent["headers"]["Authorization"] == "Bearer jev-key"
        assert sent["body"]["state"] == "Exchange hacked\n\nFunds gone"
        assert sent["body"]["model"] == "jev-latest"
        assert set(sent["body"]["questions"]) == {"asset", "direction", "impact"}


class TestNewsSignal:
    def evaluate(self, *items, window=120):
        config = StrategyConfig(news_window_minutes=window)
        context = SignalContext("BTC-USD", candles=[], config=config, news=list(items))
        return NewsSignal().evaluate(context)

    def test_no_news_is_no_opinion(self):
        assert self.evaluate() is None

    def test_the_strongest_recent_headline_decides(self):
        signal = self.evaluate(labeled("bullish", impact=1.2), labeled("bearish", impact=3.0))
        assert signal.score < 0

    def test_confidence_decays_across_the_window(self):
        item = labeled(minutes_ago=60)
        signal = self.evaluate(item, window=120)
        assert signal.confidence == pytest.approx(item.confidence * 0.5, abs=0.01)

    def test_neutral_minor_and_expired_items_carry_no_opinion(self):
        assert self.evaluate(labeled("neutral")) is None
        assert self.evaluate(labeled("bullish", impact=0.5)) is None
        assert self.evaluate(labeled("bullish", minutes_ago=200)) is None

    def test_market_wide_news_applies_to_every_symbol(self):
        assert labeled(asset="MARKET").applies_to("ETH-USD")
        assert not labeled(asset="BTC").applies_to("ETH-USD")


class TestCompositeWithNews:
    PRICE_SOURCES = [TrendSignal(), MomentumSignal(), MeanReversionSignal(), BreakoutSignal()]

    def test_a_quiet_news_day_leaves_the_price_blend_untouched(self):
        candles = make_candles(uptrend())
        price_weights = {
            regime: {k: v for k, v in weights.items() if k != "news"}
            for regime, weights in StrategyConfig().weights.items()
        }
        price_only = CompositeStrategy(
            StrategyConfig(weights=price_weights), sources=self.PRICE_SOURCES
        ).evaluate("BTC-USD", candles)
        with_news = CompositeStrategy(StrategyConfig()).evaluate("BTC-USD", candles)
        assert with_news.score == pytest.approx(price_only.score)
        assert with_news.confidence == pytest.approx(price_only.confidence)
        assert "news" not in with_news.weights

    def test_bearish_news_pulls_a_bullish_view_down(self):
        candles = make_candles(uptrend())
        strategy = CompositeStrategy(StrategyConfig())
        quiet = strategy.evaluate("BTC-USD", candles)
        newsy = strategy.evaluate("BTC-USD", candles, news=[labeled("bearish")])
        assert newsy.score < quiet.score
        assert "news" in [s.name for s in newsy.signals]
