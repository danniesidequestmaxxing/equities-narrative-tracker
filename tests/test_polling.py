"""twitterapi.io polling provider parsing + pagination (recorded JSON, no network)."""

from datetime import datetime, timezone

import pytest

from narrative_tracker.config import Settings
from narrative_tracker.ingest.polling_client import TwitterApiIoPollingProvider

NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)


async def test_polling_parses_and_queries_correctly():
    seen = {}

    async def fake_fetch(path, params, headers):
        seen.update(path=path, query=params.get("query"), key=headers.get("X-API-Key"))
        return {
            "tweets": [
                {"id": "1", "text": "$NVDA breaking out", "createdAt": "2026-06-03 14:00:00",
                 "author": {"id": "111", "userName": "whale"}}
            ],
            "has_next_page": False,
        }

    prov = TwitterApiIoPollingProvider(api_key="k", handles=["whale"], fetch=fake_fetch, now=lambda: NOW)
    posts = [p async for p in prov._poll_handle("whale", fake_fetch)]

    assert "advanced_search" in seen["path"]
    assert "from:whale" in seen["query"] and seen["key"] == "k"
    assert len(posts) == 1
    assert posts[0].platform_post_id == "1"
    assert posts[0].platform_user_id == "whale"  # keyed by handle (lowercased)
    assert posts[0].handle == "whale"
    assert "$NVDA" in posts[0].text


async def test_handles_provider_is_used():
    async def provider():
        return ["@Alpha", "bravo"]

    prov = TwitterApiIoPollingProvider(api_key="k", handles_provider=provider)
    assert await prov._current_handles() == ["Alpha", "bravo"]


async def test_polling_follows_pagination():
    pages = [
        {"tweets": [{"id": "1", "text": "a", "author": {"id": "1", "userName": "w"}}], "has_next_page": True, "next_cursor": "c1"},
        {"tweets": [{"id": "2", "text": "b", "author": {"id": "1", "userName": "w"}}], "has_next_page": False},
    ]
    calls = {"n": 0}

    async def fake_fetch(path, params, headers):
        page = pages[min(calls["n"], 1)]
        calls["n"] += 1
        return page

    prov = TwitterApiIoPollingProvider(api_key="k", handles=["w"], fetch=fake_fetch, now=lambda: NOW)
    posts = [p async for p in prov._poll_handle("w", fake_fetch)]
    assert {p.platform_post_id for p in posts} == {"1", "2"}


async def test_polling_requires_api_key():
    prov = TwitterApiIoPollingProvider(api_key=None, handles=["w"])
    with pytest.raises(RuntimeError):
        async for _ in prov.stream():
            break


def test_watchlist_handles_parsing():
    s = Settings(_env_file=None, watchlist=" blknoiz06, @elonmusk ,, macro_mike ")
    assert s.watchlist_handles == ["blknoiz06", "elonmusk", "macro_mike"]


def test_to_rawpost_tags_replies_and_retweets():
    # The $RDDT incident: a REPLY tweet (hidden from the profile timeline) fired
    # an alert that looked unverifiable. Replies must be tagged as such.
    from narrative_tracker.ingest.polling_client import _to_rawpost

    reply = _to_rawpost({
        "id": "2064943854068060238", "isReply": True, "inReplyToId": "2064",
        "text": "@Ud197601 Everyone kept calling $AXTI a scam, and my thesis BS on $RDDT.",
        "createdAt": "2026-06-11 05:32:34", "author": {"id": "9", "userName": "aleabitoreddit"},
    })
    assert reply.post_type == "reply"

    # bare @-leading text still counts as a reply even without explicit flags
    bare = _to_rawpost({"id": "2", "text": "@someone $HOOD all green",
                        "createdAt": "2026-06-11 05:33:00", "author": {"userName": "a"}})
    assert bare.post_type == "reply"

    rt = _to_rawpost({"id": "3", "text": "RT @x: $NVDA", "retweeted_tweet": {"id": "9"},
                      "createdAt": "2026-06-11 05:34:00", "author": {"userName": "a"}})
    assert rt.post_type == "retweet"

    orig = _to_rawpost({"id": "4", "text": "$NVDA breaking out",
                        "createdAt": "2026-06-11 05:35:00", "author": {"userName": "a"}})
    assert orig.post_type == "original"
