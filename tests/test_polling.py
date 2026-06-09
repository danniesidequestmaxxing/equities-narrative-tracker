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
    assert posts[0].platform_user_id == "111"
    assert posts[0].handle == "whale"
    assert "$NVDA" in posts[0].text


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
