from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from .http import post_json
from .models import ArticleRecord
from .utils import (
    canonicalize_url,
    clean_text,
    make_content_fingerprint,
    make_content_hash,
    make_dedup_key,
    make_resolved_target,
    normalize_title,
    parse_datetime,
    url_host,
    utc_now,
)


class TavilyCollector:
    """Search-based gap filler; results still pass through the normal repository deduper."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 15,
        user_agent: str = "ainews-open",
        json_post: Callable[..., Dict[str, object]] = post_json,
    ):
        self.api_key = clean_text(api_key)
        self.timeout = timeout
        self.user_agent = user_agent
        self.json_post = json_post

    def collect(self, queries: Iterable[str], *, max_results: int = 10) -> List[ArticleRecord]:
        articles: List[ArticleRecord] = []
        for query in queries:
            normalized_query = clean_text(query)
            if not normalized_query:
                continue
            response = self.json_post(
                "https://api.tavily.com/search",
                {
                    "api_key": self.api_key,
                    "query": normalized_query,
                    "topic": "news",
                    "search_depth": "advanced",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
                timeout=self.timeout,
                user_agent=self.user_agent,
            )
            for item in response.get("results", []):
                if not isinstance(item, dict):
                    continue
                article = self._article(item, query=normalized_query)
                if article is not None:
                    articles.append(article)
        return articles

    @staticmethod
    def _article(item: Dict[str, object], *, query: str) -> Optional[ArticleRecord]:
        title = clean_text(str(item.get("title", "")))
        url = clean_text(str(item.get("url", "")))
        summary = clean_text(str(item.get("content", "")))
        if not title or not url:
            return None
        canonical_url = canonicalize_url(url)
        source_name = url_host(url) or "Tavily Search"
        return ArticleRecord(
            source_id="tavily-search",
            source_name=source_name,
            title=title,
            url=url,
            canonical_url=canonical_url,
            summary=summary,
            published_at=parse_datetime(str(item.get("published_date", ""))),
            discovered_at=utc_now(),
            language="auto",
            region="global",
            country="global",
            topic="ai",
            content_hash=make_content_hash(title, summary),
            dedup_key=make_dedup_key(title),
            normalized_title=normalize_title(title),
            resolved_target=make_resolved_target(url, canonical_url),
            content_fingerprint=make_content_fingerprint(title, summary),
            raw_payload={"provider": "tavily", "query": query, **item},
        )
