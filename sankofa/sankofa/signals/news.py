"""Collectors that produce TextItems: RSS feeds, Google News RSS search, and Reddit.

No HTML scraping of sites' pages: RSS and Reddit's JSON endpoints are the stable surfaces.
Reddit has two modes: "public" (reddit.com/r/<sub>/new.json, no credentials, rate-limited)
and "praw" (official API with a free script app). Reddit's API terms changed in 2023; verify
the current free-tier limits before scaling the request volume.
"""
from __future__ import annotations

import logging
import os
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


class RedditCollector:
    """Reddit posts (and optionally top comments) from subreddits and site-wide searches."""

    PUBLIC_BASE = "https://www.reddit.com"

    def __init__(self, cfg: dict, lookback_hours: int = 24) -> None:
        self.mode = str(cfg.get("mode", "public")).lower()
        self.subreddits = [str(x) for x in cfg.get("subreddits", [])]
        self.queries = [str(x) for x in cfg.get("search_queries", [])]
        self.limit = max(5, min(100, int(cfg.get("posts_per_source", 50))))
        self.include_comments = bool(cfg.get("include_comments", False))
        self.min_score = int(cfg.get("min_score", 1))
        self.boosted = {str(u).lower().lstrip("u/") for u in (cfg.get("boosted_users") or [])}
        self.lookback = timedelta(hours=lookback_hours)
        self.user_agent = os.environ.get("REDDIT_USER_AGENT", "sankofa-bot/0.1 (commodity sentiment research)")

    # ---- shared ----------------------------------------------------------------
    def _weight(self, score: int, num_comments: int, author: str) -> float:
        # engagement-weighted: 100 upvotes or 50 comments doubles the weight, capped at 3x
        w = 1.0 + min(2.0, score / 100 + num_comments / 50)
        if author.lower() in self.boosted:
            w *= 3.0
        return w

    def _item(self, title: str, body: str, score: int, num_comments: int, author: str,
              created_utc: float, permalink: str, origin: str) -> TextItem | None:
        created = datetime.fromtimestamp(float(created_utc), tz=UTC)
        if created < datetime.now(UTC) - self.lookback or score < self.min_score:
            return None
        text = f"{title}. {(body or '')[:600]}".strip()
        return TextItem(text=text, source=f"reddit:{origin}", published=created,
                        weight=self._weight(score, num_comments, author),
                        url=f"{self.PUBLIC_BASE}{permalink}" if permalink.startswith("/") else permalink)

    # ---- public JSON mode ---------------------------------------------------------
    def _public_get(self, path: str, params: dict) -> list[dict]:
        import requests

        try:
            r = requests.get(f"{self.PUBLIC_BASE}{path}", params=params, timeout=15,
                             headers={"User-Agent": self.user_agent})
            if r.status_code == 429:
                log.warning("reddit rate-limited (429) on %s; backing off this cycle", path)
                return []
            r.raise_for_status()
            return [c["data"] for c in r.json().get("data", {}).get("children", []) if c.get("kind") == "t3"]
        except Exception as exc:
            log.warning("reddit fetch failed %s: %s", path, exc)
            return []

    def _collect_public(self) -> list[TextItem]:
        items: list[TextItem] = []
        calls = 0
        for sub in self.subreddits:
            for d in self._public_get(f"/r/{sub}/new.json", {"limit": self.limit, "raw_json": 1}):
                it = self._item(d.get("title", ""), d.get("selftext", ""), int(d.get("score", 0)),
                                int(d.get("num_comments", 0)), str(d.get("author", "")),
                                d.get("created_utc", 0), d.get("permalink", ""), f"r/{sub}")
                if it:
                    items.append(it)
            calls += 1
            time.sleep(1.0)  # stay well under the unauthenticated rate limit
        for q in self.queries:
            for d in self._public_get("/search.json", {"q": q, "sort": "new", "t": "day",
                                                       "limit": self.limit, "raw_json": 1}):
                it = self._item(d.get("title", ""), d.get("selftext", ""), int(d.get("score", 0)),
                                int(d.get("num_comments", 0)), str(d.get("author", "")),
                                d.get("created_utc", 0), d.get("permalink", ""), f"search:{q[:30]}")
                if it:
                    items.append(it)
            calls += 1
            time.sleep(1.0)
        return items

    # ---- PRAW mode ---------------------------------------------------------------------
    def _collect_praw(self) -> list[TextItem]:
        try:
            import praw
        except ImportError:
            log.warning("praw not installed; pip install 'sankofa[reddit]' or use mode: public")
            return []
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            log.warning("REDDIT_CLIENT_ID/SECRET missing; falling back to public mode")
            return self._collect_public()
        reddit = praw.Reddit(client_id=cid, client_secret=secret, user_agent=self.user_agent)
        items: list[TextItem] = []

        def add(sub, origin: str) -> None:
            author = str(sub.author) if sub.author else ""
            it = self._item(sub.title, getattr(sub, "selftext", ""), int(sub.score), int(sub.num_comments),
                            author, sub.created_utc, sub.permalink, origin)
            if not it:
                return
            items.append(it)
            if self.include_comments:
                try:
                    sub.comments.replace_more(limit=0)
                    for c in sub.comments[:10]:
                        if c.score >= self.min_score and c.body:
                            items.append(TextItem(text=c.body[:600], source=f"reddit:{origin}:comment",
                                                  published=datetime.fromtimestamp(c.created_utc, tz=UTC),
                                                  weight=0.5 * self._weight(int(c.score), 0, str(c.author or ""))))
                except Exception as exc:
                    log.debug("comment fetch failed: %s", exc)

        for name in self.subreddits:
            try:
                for sub in reddit.subreddit(name).new(limit=self.limit):
                    add(sub, f"r/{name}")
            except Exception as exc:
                log.warning("reddit r/%s failed: %s", name, exc)
        for q in self.queries:
            try:
                for sub in reddit.subreddit("all").search(q, sort="new", time_filter="day", limit=self.limit):
                    add(sub, f"search:{q[:30]}")
            except Exception as exc:
                log.warning("reddit search %r failed: %s", q, exc)
        return items

    def collect(self) -> list[TextItem]:
        items = self._collect_praw() if self.mode == "praw" else self._collect_public()
        log.info("Reddit: %d items from %d subreddits + %d searches (%s mode)",
                 len(items), len(self.subreddits), len(self.queries), self.mode)
        return items
