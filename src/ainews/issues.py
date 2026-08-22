from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from .models import CanonicalIssue, IssueItem
from .utils import clean_text, parse_datetime, truncate_text, utc_now


class IssueBuilder:
    def __init__(
        self,
        *,
        public_base_url: str,
        timezone: str = "Asia/Shanghai",
        min_items: int = 5,
        max_items: int = 10,
        cutoff_hour: int = 5,
    ):
        self.public_base_url = public_base_url.rstrip("/")
        self.timezone = ZoneInfo(timezone)
        self.min_items = max(1, min_items)
        self.max_items = max(self.min_items, max_items)
        self.cutoff_hour = min(23, max(0, cutoff_hour))

    def build(
        self,
        payload: Dict[str, object],
        *,
        window_end: Optional[datetime] = None,
    ) -> CanonicalIssue:
        end = window_end or self._latest_cutoff(utc_now())
        if end.tzinfo is None:
            end = end.replace(tzinfo=ZoneInfo("UTC"))
        end = end.astimezone(self.timezone)
        lookback = max(1, int(payload.get("since_hours") or 24))
        start = end - timedelta(hours=lookback)
        issue_date = end.date().isoformat()
        slug = issue_date
        public_url = (
            f"{self.public_base_url}/issues/{slug}" if self.public_base_url else f"/issues/{slug}"
        )

        digest = payload.get("digest") if isinstance(payload.get("digest"), dict) else {}
        selection_preview = [
            item for item in (payload.get("selection_preview") or []) if isinstance(item, dict)
        ]
        selected_ids = self._selected_article_ids(selection_preview)
        articles = [
            item
            for item in (payload.get("articles") or [])
            if isinstance(item, dict) and self._inside_window(item, start=start, end=end)
        ]
        article_map = {int(item.get("id") or 0): item for item in articles}
        selected = [
            (article_map[article_id], preview)
            for preview in selection_preview
            if (article_id := int(preview.get("article_id") or 0)) in article_map
        ]
        if not selected_ids:
            selected = [(article, {}) for article in articles]
        items = [
            self._issue_item(article, preview) for article, preview in selected[: self.max_items]
        ]

        title = clean_text(str(digest.get("title") or f"{issue_date} AI 新闻日报"))
        overview = clean_text(str(digest.get("overview") or ""))
        highlights = [
            clean_text(str(item)) for item in digest.get("highlights", []) if clean_text(str(item))
        ]
        share_summary = self._share_summary(overview, highlights, items)
        status = "ready" if len(items) >= self.min_items else "blocked"
        return CanonicalIssue(
            issue_date=issue_date,
            slug=slug,
            title=title,
            overview=overview,
            share_summary=share_summary,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            public_url=public_url,
            status=status,
            items=items,
        )

    def _latest_cutoff(self, reference: datetime) -> datetime:
        local = reference.astimezone(self.timezone)
        cutoff = local.replace(hour=self.cutoff_hour, minute=0, second=0, microsecond=0)
        if local < cutoff:
            cutoff -= timedelta(days=1)
        return cutoff

    @staticmethod
    def _selected_article_ids(items: Iterable[object]) -> set[int]:
        return {
            int(item.get("article_id") or 0)
            for item in items
            if isinstance(item, dict) and int(item.get("article_id") or 0) > 0
        }

    def _inside_window(
        self,
        article: Dict[str, object],
        *,
        start: datetime,
        end: datetime,
    ) -> bool:
        published = parse_datetime(str(article.get("published_at") or ""))
        return start <= published.astimezone(self.timezone) < end

    @staticmethod
    def _issue_item(
        article: Dict[str, object], selection: Optional[Dict[str, object]] = None
    ) -> IssueItem:
        selected = selection or {}
        return IssueItem(
            article_id=int(article.get("id") or 0),
            title=clean_text(
                str(
                    selected.get("title")
                    or article.get("display_title_zh")
                    or article.get("title")
                    or ""
                )
            ),
            summary=clean_text(
                str(
                    selected.get("publish_summary")
                    or article.get("compact_summary_zh")
                    or article.get("display_summary_zh")
                    or ""
                )
            ),
            importance=clean_text(
                str(article.get("llm_brief_zh") or article.get("importance_zh") or "")
            ),
            source_name=clean_text(str(article.get("source_name") or "")),
            source_url=clean_text(str(article.get("url") or "")),
            published_at=clean_text(str(article.get("published_at") or "")),
            category=clean_text(str(selected.get("section") or article.get("topic") or "news"))
            or "news",
            rank_score=float(selected.get("rank_score") or article.get("rank_score") or 0.0),
        )

    @staticmethod
    def _share_summary(overview: str, highlights: List[str], items: List[IssueItem]) -> str:
        parts = [overview] if overview else []
        selected = highlights[:3] or [item.title for item in items[:3]]
        if selected:
            parts.append("重点：" + "；".join(selected))
        return truncate_text(" ".join(parts), 300)
