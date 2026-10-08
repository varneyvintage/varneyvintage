import time
from datetime import UTC, datetime

from sankofa.signals.news import RedditCollector


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _listing(*posts):
    return {"data": {"children": [{"kind": "t3", "data": p} for p in posts]}}


def test_public_mode_parses_posts_and_weights(monkeypatch):
    now = datetime.now(UTC).timestamp()
    fresh = {"title": "Cocoa shortage in Ghana", "selftext": "Cocobod warns", "score": 120, "num_comments": 30,
             "author": "trader1", "created_utc": now - 600, "permalink": "/r/commodities/abc"}
    old = dict(fresh, title="old post", created_utc=now - 3 * 86400)
    low = dict(fresh, title="downvoted", score=-5)
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params))
        assert "User-Agent" in headers
        return _Resp(_listing(fresh, old, low))

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    rc = RedditCollector({"mode": "public", "subreddits": ["commodities"], "search_queries": ["cocoa Ghana"],
                          "boosted_users": ["trader1"], "min_score": 1}, lookback_hours=24)
    items = rc.collect()
    assert len(items) == 2  # one from the subreddit, one from the search; old + downvoted dropped
    assert all(it.source.startswith("reddit:") for it in items)
    # engagement (1 + 1.2 + 0.6 = 2.8) times the 3x boost
    assert abs(items[0].weight - 2.8 * 3) < 1e-9
    assert calls[0][0].endswith("/r/commodities/new.json")
    assert calls[1][1]["q"] == "cocoa Ghana"


def test_rate_limit_is_tolerated(monkeypatch):
    class R429:
        status_code = 429
        def raise_for_status(self): raise AssertionError("should not be called")
        def json(self): return {}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R429())
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    rc = RedditCollector({"mode": "public", "subreddits": ["gold"]})
    assert rc.collect() == []
