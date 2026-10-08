"""Collectors that produce TextItems: RSS feeds, Google News RSS search, and (optional) X/Twitter.

No HTML scraping of sites' pages: RSS is the stable, ToS-friendly surface. For X, the v2 API
is used via tweepy if a bearer token is present; the recent-search endpoint sits on a paid
tier - verify current pricing before budgeting around it.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urlparse

from .sentiment import TextItem

log = logging.getLogger(__name__)


def _parse_time(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        st = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=UTC)
    return datetime.now(UTC)


class RSSCollector:
    def __init__(self, feeds: list[str], lookback_hours: int = 24, timeout: int = 15) -> None:
        self.feeds = list(feeds)
        self.lookback = timedelta(hours=lookback_hours)
        self.timeout = timeout

    def collect(self) -> list[TextItem]:
        import feedparser
        import requests

        cutoff = datetime.now(UTC) - self.lookback
        items: list[TextItem] = []
        for url in self.feeds:
            try:
                resp = requests.get(url, timeout=self.timeout,
                                    headers={"User-Agent": "sankofa-bot/0.1 (+rss)"})
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as exc:
                log.warning("RSS fetch failed %s: %s", url, exc)
                continue
            domain = urlparse(url).netloc
            if not parsed.entries:
                log.warning("RSS feed %s returned no entries (blocked, changed URL, or empty)", url)
            for e in parsed.entries:
                ts = _parse_time(e)
                if ts < cutoff:
                    continue
                title = getattr(e, "title", "") or ""
                summary = getattr(e, "summary", "") or ""
                text = f"{title}. {summary}".strip()
                if text:
                    items.append(TextItem(text=text, source=f"rss:{domain}", published=ts,
                                          url=getattr(e, "link", "")))
        log.info("RSS: %d items from %d feeds", len(items), len(self.feeds))
        return items


class GoogleNewsCollector(RSSCollector):
    """Google News exposes an RSS feed per search query. Free, no key, works well for topics."""

    def __init__(self, queries: list[str], lookback_hours: int = 24, lang: str = "en", region: str = "US") -> None:
        feeds = [
            f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={lang}&gl={region}&ceid={region}:{lang}"
            for q in queries
        ]
        super().__init__(feeds, lookback_hours=lookback_hours)
        self.queries = queries

    def collect(self) -> list[TextItem]:
        items = super().collect()
        for it in items:
            it.source = it.source.replace("rss:news.google.com", "gnews")
        return items


class XCollector:
    """Optional X/Twitter v2 recent search. Requires X_BEARER_TOKEN and the [x] extra."""

    def __init__(self, bearer_token: str, queries: list[str], max_results: int = 50,
                 boosted_accounts: list[str] | None = None, lookback_hours: int = 24) -> None:
        self.token = bearer_token
        self.queries = queries
        self.max_results = max(10, min(100, int(max_results)))
        self.boosted = {h.lower().lstrip("@") for h in (boosted_accounts or [])}
        self.lookback = timedelta(hours=lookback_hours)

    def collect(self) -> list[TextItem]:
        try:
            import tweepy
        except ImportError:
            log.warning("tweepy not installed; pip install 'sankofa[x]' to enable X collection")
            return []
        client = tweepy.Client(bearer_token=self.token, wait_on_rate_limit=True)
        start = datetime.now(UTC) - self.lookback
        items: list[TextItem] = []
        for q in self.queries:
            try:
                resp = client.search_recent_tweets(
                    query=q, max_results=self.max_results, start_time=start,
                    tweet_fields=["created_at", "author_id", "public_metrics"],
                    expansions=["author_id"], user_fields=["username", "verified"],
                )
            except Exception as exc:
                log.warning("X search failed for %r: %s", q, exc)
                continue
            users = {u.id: u for u in (resp.includes or {}).get("users", [])}
            for t in resp.data or []:
                u = users.get(t.author_id)
                handle = (u.username if u else str(t.author_id)).lower()
                metrics = t.public_metrics or {}
                # engagement-weighted, boosted accounts count 3x
                w = 1.0 + min(2.0, (metrics.get("like_count", 0) + 2 * metrics.get("retweet_count", 0)) / 200)
                if handle in self.boosted:
                    w *= 3.0
                items.append(TextItem(text=t.text, source=f"x:{handle}", published=t.created_at or datetime.now(UTC),
                                      weight=w))
        log.info("X: %d posts from %d queries", len(items), len(self.queries))
        return items
